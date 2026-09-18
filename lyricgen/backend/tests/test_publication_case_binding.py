"""Publication effects are bound to one reviewed cut and one explicit case.

Uses the real HTTP/DB publication path and an in-memory object store. Hooks
interleave edits/new requests at I/O boundaries; no real storage or jobs run.
"""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
from unittest.mock import Mock

import pytest

import database
from database import Delivery, DeliveryChangeRequest, Job
from delivery_freshness import render_fingerprint
from delivery_snapshots import FILENAMES, working_key
from tests.conftest import auth
from tests.test_deliveries import approved_job  # noqa: F401 -- shared fixture


@pytest.fixture
def fake_r2(monkeypatch, approved_job):
    import storage

    state = {"objects": {}, "head_hook": None, "copy_hook": None,
             "heads": [], "copies": [], "unknown": False}
    for file_type in FILENAMES:
        key = working_key(approved_job.tenant_id, approved_job.job_id, file_type)
        state["objects"][key] = ("render-A-" + file_type).encode()

    def identity(key):
        data = state["objects"].get(key)
        if data is None:
            return {"status": "missing"}
        return {"status": "exists", "etag": hashlib.sha256(data).hexdigest(),
                "size": len(data)}

    def head(key):
        state["heads"].append(key)
        if state["head_hook"]:
            state["head_hook"](key)
        return "unknown" if state["unknown"] else identity(key)["status"]

    def copy(source, target, *, expected_etag=None):
        state["copies"].append((source, target))
        if state["copy_hook"]:
            state["copy_hook"](source, target)
        if identity(source).get("etag") != expected_etag:
            return False
        state["objects"][target] = state["objects"][source]
        return True

    monkeypatch.setattr(storage, "object_status_bounded", head)
    monkeypatch.setattr(storage, "object_status", lambda key: identity(key)["status"])
    monkeypatch.setattr(storage, "object_identity", identity)
    monkeypatch.setattr(storage, "copy_object", copy)
    monkeypatch.setattr(storage, "object_exists", lambda key: key in state["objects"])
    return state


def _publish(client, token, job_id, **body):
    return client.post(f"/admin/deliveries/from-job/{job_id}",
                       headers=auth(token), json={"portal_id": "argentina", **body})


def _prepare(client, token, db, job, fake_r2):
    job_id = job.job_id
    initial = _publish(client, token, job_id)
    assert initial.status_code == 200, initial.text
    delivery_id = initial.json()["delivery_id"]
    requests = [DeliveryChangeRequest(
        delivery_id=delivery_id, comment=comment,
        submitted_at=datetime.now(timezone.utc) - timedelta(minutes=2),
    ) for comment in ("Corregir letra", "Cambiar fondo")]
    db.add_all(requests)
    db.flush()
    ids = [request.id for request in requests]
    current = db.query(Job).filter(Job.job_id == job_id).one()
    current.edit_count = (current.edit_count or 0) + 1
    current.segments_revision = (current.segments_revision or 0) + 1
    current.render_params = {
        **(current.render_params or {}),
        "_rendered_segments_revision": current.segments_revision,
        "_rendered_at": datetime.now(timezone.utc).isoformat(),
    }
    current.s3_keys = {ft: working_key(current.tenant_id, job_id, ft) for ft in FILENAMES}
    body = {"change_request_id": ids[0],
            "reviewed_editor_revision": current.segments_revision,
            "reviewed_render_fingerprint": render_fingerprint(current)}
    for ft in FILENAMES:
        fake_r2["objects"][working_key(current.tenant_id, job_id, ft)] = ("render-B-" + ft).encode()
    db.commit()
    db.rollback()
    fake_r2["copies"].clear()
    fake_r2["heads"].clear()
    return job_id, delivery_id, ids, body


def _assert_open(db, request_ids):
    db.expire_all()
    for request_id in request_ids:
        request = db.get(DeliveryChangeRequest, request_id)
        assert request.resolved_at is None
        assert request.resolved_by_revision is None
        assert request.resolution_source is None
    db.rollback()


def test_history_publication_never_attests_pending_cases(
    client, admin_token, approved_job, db, fake_r2,
):
    job_id, delivery_id, ids, _ = _prepare(client, admin_token, db, approved_job, fake_r2)
    response = _publish(client, admin_token, job_id)
    assert response.status_code == 200, response.text
    assert response.json()["revision"] == 2
    assert response.json()["resolved_change_requests"] == []
    _assert_open(db, ids)
    row = db.get(Delivery, delivery_id)
    assert row.published_revision == 2
    assert all(fake_r2["objects"][key].startswith(b"render-B-")
               for key in row.published_file_keys.values())


def test_reviewed_publication_closes_only_selected_case_including_new_during_copy(
    client, admin_token, approved_job, db, fake_r2,
):
    job_id, delivery_id, ids, body = _prepare(client, admin_token, db, approved_job, fake_r2)
    inserted = []

    def add_new_request(_source, _target):
        if inserted:
            return
        with database.SessionLocal() as concurrent:
            request = DeliveryChangeRequest(
                delivery_id=delivery_id, comment="Nueva instrucción no revisada",
                submitted_at=datetime.now(timezone.utc),
            )
            concurrent.add(request)
            concurrent.flush()
            inserted.append(request.id)
            concurrent.commit()

    fake_r2["copy_hook"] = add_new_request
    response = _publish(client, admin_token, job_id, **body)
    assert response.status_code == 200, response.text
    assert inserted and response.json()["resolved_change_requests"] == [ids[0]]
    db.expire_all()
    selected = db.get(DeliveryChangeRequest, ids[0])
    assert selected.resolved_at is not None
    assert selected.resolved_by_revision == response.json()["revision"] == 2
    assert selected.resolution_source == "publication"
    _assert_open(db, [ids[1], inserted[0]])


@pytest.mark.parametrize("stale_field,stale_value", [
    ("reviewed_render_fingerprint", "not-the-reviewed-cut"),
    ("reviewed_editor_revision", 999),
])
def test_stale_review_identity_does_not_copy_publish_or_close(
    client, admin_token, approved_job, db, fake_r2, stale_field, stale_value,
):
    job_id, delivery_id, ids, body = _prepare(client, admin_token, db, approved_job, fake_r2)
    before = deepcopy(db.get(Delivery, delivery_id).published_file_keys)
    db.rollback()
    body[stale_field] = stale_value
    response = _publish(client, admin_token, job_id, **body)
    assert response.status_code == 409, response.text
    assert fake_r2["copies"] == []
    _assert_open(db, ids)
    row = db.get(Delivery, delivery_id)
    assert row.published_revision == 1 and row.published_file_keys == before


def test_preflight_heads_hold_no_transaction_and_run_off_event_loop(
    client, admin_token, approved_job, db, fake_r2,
):
    import main

    sessions = []
    original_overrides = dict(main.app.dependency_overrides)

    def tracked_session():
        session = database.SessionLocal()
        sessions.append(session)
        try:
            yield session
        finally:
            session.rollback()
            session.close()

    def check_head(_key):
        assert sessions and all(not session.in_transaction() for session in sessions)
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()

    main.app.dependency_overrides[main.get_db] = tracked_session
    main.app.dependency_overrides[main.get_deliveries_db] = tracked_session
    fake_r2["head_hook"] = check_head
    try:
        response = _publish(client, admin_token, approved_job.job_id)
        assert response.status_code == 200, response.text
        assert len(fake_r2["heads"]) == len(FILENAMES)
    finally:
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(original_overrides)


def test_unknown_storage_does_not_enqueue_or_change_publication(
    client, admin_token, approved_job, db, fake_r2, monkeypatch,
):
    job_id, delivery_id, ids, body = _prepare(client, admin_token, db, approved_job, fake_r2)
    before = deepcopy(db.get(Delivery, delivery_id).published_file_keys)
    db.rollback()
    fake_r2["unknown"] = True
    enqueue = Mock()
    monkeypatch.setattr("main.enqueue_prores_prewarm", enqueue)
    response = _publish(client, admin_token, job_id, **body)
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "publication_storage_unavailable"
    enqueue.assert_not_called()
    assert fake_r2["copies"] == []
    _assert_open(db, ids)
    row = db.get(Delivery, delivery_id)
    assert row.published_revision == 1 and row.published_file_keys == before


def test_edit_during_head_conflicts_without_copying_or_resolving(
    client, admin_token, approved_job, db, fake_r2,
):
    job_id, delivery_id, ids, body = _prepare(client, admin_token, db, approved_job, fake_r2)
    before = deepcopy(db.get(Delivery, delivery_id).published_file_keys)
    db.rollback()
    edited = []

    def edit(_key):
        if edited:
            return
        with database.SessionLocal() as concurrent:
            job = concurrent.query(Job).filter(Job.job_id == job_id).one()
            job.segments_revision += 1
            concurrent.commit()
        edited.append(True)

    fake_r2["head_hook"] = edit
    response = _publish(client, admin_token, job_id, **body)
    assert response.status_code == 409, response.text
    assert edited and fake_r2["copies"] == []
    _assert_open(db, ids)
    row = db.get(Delivery, delivery_id)
    assert row.published_revision == 1 and row.published_file_keys == before


@pytest.mark.parametrize("note", [None, "", "   \n\t"])
def test_manual_resolution_requires_nonblank_reason(
    client, admin_token, approved_job, db, fake_r2, note,
):
    _, _, ids, _ = _prepare(client, admin_token, db, approved_job, fake_r2)
    response = client.post(
        f"/admin/change-requests/{ids[0]}/resolve", headers=auth(admin_token),
        json={} if note is None else {"resolution_note": note},
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "resolution_reason_required"
    _assert_open(db, ids)


@pytest.mark.parametrize('concurrency', [2, 8])
def test_same_pool_concurrent_publication_has_one_revision_switch(
    client, admin_token, approved_job, db, fake_r2, concurrency,
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Lock, get_ident

    if db.bind.dialect.name != 'postgresql':
        pytest.skip('Publication row locks and pooled contention require PostgreSQL')
    assert database.DeliveriesSessionLocal.kw['bind'] is database.SessionLocal.kw['bind']
    job_id, delivery_id, ids, body = _prepare(client, admin_token, db, approved_job, fake_r2)
    start = Barrier(concurrency, timeout=15)
    copying = Barrier(concurrency, timeout=15)
    seen_threads, guard = set(), Lock()

    def first_copy(_source, _target):
        # Force every request to prepare against v1 before anyone can switch
        # the pointer. At this barrier no publisher may retain a DB connection.
        with guard:
            first = get_ident() not in seen_threads
            seen_threads.add(get_ident())
        if first:
            copying.wait()

    fake_r2['copy_hook'] = first_copy

    def submit(_):
        start.wait()
        return _publish(client, admin_token, job_id, **body)

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        responses = list(pool.map(submit, range(concurrency)))
    assert sorted(response.status_code for response in responses) == [200] + [409] * (concurrency - 1), [
        (response.status_code, response.text) for response in responses
    ]
    db.expire_all()
    delivery = db.get(Delivery, delivery_id)
    assert delivery.published_revision == 2
    assert db.get(DeliveryChangeRequest, ids[0]).resolved_by_revision == 2
    assert all(fake_r2['objects'][key].startswith(b'render-B-')
               for key in delivery.published_file_keys.values())
    _assert_open(db, [ids[1]])


@pytest.mark.parametrize('concurrency', [2, 8])
def test_pending_prores_concurrent_replays_release_locks_and_keep_portal_unchanged(
    client, admin_token, approved_job, db, fake_r2, monkeypatch, concurrency,
):
    import main
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, local
    from sqlalchemy import event

    if db.bind.dialect.name != 'postgresql':
        pytest.skip('Publication row locks and pooled contention require PostgreSQL')
    job_id, delivery_id, ids, body = _prepare(client, admin_token, db, approved_job, fake_r2)
    before = deepcopy(db.get(Delivery, delivery_id).published_file_keys)
    db.rollback()
    missing_objects = {key: fake_r2['objects'].pop(key) for key in list(fake_r2['objects'])
                       if key.endswith(('umg_master.mov', 'umg_short.mov'))}
    sessions = []
    thread_state = local()
    saved_overrides = dict(main.app.dependency_overrides)

    def tracked_session():
        session = database.SessionLocal()
        sessions.append(session)
        event.listen(session, 'do_orm_execute',
                     lambda _state: setattr(thread_state, 'last_session', session))
        try:
            yield session
        finally:
            session.rollback()
            session.close()

    # Check the caller's last SQL session, not concurrent requests legitimately
    # holding their own short DB transaction at this moment.
    real_enqueue_calls = []

    def enqueue(_job_id, file_type, *, force=False):
        assert force
        assert not thread_state.last_session.in_transaction()
        # Queue work must not run on the application loop.
        with pytest.raises(RuntimeError, match='no running event loop'):
            asyncio.get_running_loop()
        real_enqueue_calls.append(file_type)
        return 'synthetic-prores-' + file_type

    monkeypatch.setattr(main, 'enqueue_prores_prewarm', enqueue)
    main.app.dependency_overrides[main.get_db] = tracked_session
    main.app.dependency_overrides[main.get_deliveries_db] = tracked_session
    start = Barrier(concurrency, timeout=15)

    def submit(_):
        start.wait()
        return _publish(client, admin_token, job_id, **body)

    try:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            responses = list(pool.map(submit, range(concurrency)))
        assert all(response.status_code == 202 for response in responses), [
            (response.status_code, response.text) for response in responses
        ]
        assert all(response.json()['status'] == 'preparing_prores' for response in responses)
        assert all(not session.in_transaction() for session in sessions)
        assert real_enqueue_calls and fake_r2['copies'] == []
        _assert_open(db, ids)
        delivery = db.get(Delivery, delivery_id)
        assert delivery.published_revision == 1 and delivery.published_file_keys == before
        db.rollback()
        # A later retry after workers supplied both masters can publish the
        # same reviewed intent; pending responses never claimed publication.
        fake_r2['objects'].update(missing_objects)
        completed = _publish(client, admin_token, job_id, **body)
        assert completed.status_code == 200, completed.text
        assert completed.json()['revision'] == 2
        assert completed.json()['resolved_change_requests'] == [ids[0]]
    finally:
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(saved_overrides)


def test_different_engine_delivery_session_is_not_aliased(monkeypatch):
    import main
    from fastapi import HTTPException

    local_db, external_db = Mock(), Mock()
    local_db.get_bind.return_value = object()
    external_db.get_bind.return_value = object()
    local_db.query.return_value.filter.return_value.first.return_value = object()

    def external_context(session, _request_id):
        assert session is external_db
        raise HTTPException(status_code=418, detail='external-context-observed')

    monkeypatch.setattr(main, '_change_request_context', external_context)
    with pytest.raises(HTTPException) as raised:
        main.admin_create_delivery_from_job(
            'synthetic', main.SendToUMGRequest(change_request_id=1),
            {'role': 'admin'}, local_db, external_db,
        )
    assert raised.value.status_code == 418
    local_db.rollback.assert_not_called()
    external_db.rollback.assert_not_called()


def test_publication_final_review_preserves_normal_approval_qc_gate(
    client, admin_token, approved_job, db, fake_r2, monkeypatch,
):
    monkeypatch.setenv('DELIVERY_QC_MODE', 'enforce')
    job_id, delivery_id, ids, body = _prepare(client, admin_token, db, approved_job, fake_r2)
    job = db.query(Job).filter_by(job_id=job_id).one()
    job.status = 'pending_review'
    job.approved_at = None
    job.delivery_qc = {
        'status': 'COMPLETE', 'report_id': 'blocked-publication', 'issues': [{
            'issue_id': 'objective-failure', 'status': 'OPEN', 'severity': 'FAIL',
            'code': 'MEDIA_AUDIO_STREAM_MISSING', 'result_status': 'FAIL', 'blocking': True,
        }],
    }
    db.commit()
    response = _publish(client, admin_token, job_id, **body)
    assert response.status_code == 409, response.text
    assert response.json()['detail']['code'] == 'delivery_qc_blocked'
    _assert_open(db, ids)
    assert db.get(Delivery, delivery_id).published_revision == 1
    assert db.query(Job).filter_by(job_id=job_id).one().status == 'pending_review'
    assert fake_r2['copies'] == []
