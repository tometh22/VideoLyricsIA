"""Paid background intent must reference the exact proposal preview."""
import pytest

from database import ChangeRequestProposal, DeliveryChangeRequest, Job, EditorDocument
from tests.conftest import auth
from tests.test_change_request_preview_cas import proposal_case
from tests.test_deliveries import approved_job, all_r2_files_present, _publication_copy_succeeds


@pytest.fixture
def background_case(client, admin_token, proposal_case, db, monkeypatch):
    path, _, job_id = proposal_case
    request_id = int(path.split('/')[3])
    request = db.get(DeliveryChangeRequest, request_id)
    request.comment = 'Cambiar el fondo por un paisaje de montaña sin armas.'
    job = db.query(Job).filter_by(job_id=job_id).one()
    job.bg_r2_key_cached = 'synthetic/background.mp4'
    db.commit()
    proposal = client.post(path.rsplit('/', 1)[0], headers=auth(admin_token)).json()['proposal']
    op = next(op for op in proposal['operations'] if op.get('visual_action') == 'regenerate_background')
    assert op['regeneration_supported']
    calls = []
    monkeypatch.setattr('main.enqueue_edit', lambda **kw: (calls.append(kw), 'synthetic-edit')[1])
    body = dict(edit_type='background', background_mode='imagen', background_hint='Paisaje sin armas.',
                change_request_id=request_id, change_request_proposal_id=proposal['id'],
                change_request_operation_id=op['id'], expected_proposal_hash=proposal['content_hash'],
                editor_revision=proposal['base_revision'])
    return job_id, proposal, body, calls


@pytest.mark.parametrize('mutation', ['hash', 'legacy', 'stale', 'revision', 'request', 'unsupported', 'audio'])
def test_obsolete_background_proposal_never_enqueues(client, admin_token, db, background_case, mutation):
    job_id, preview, body, calls = background_case
    row = db.get(ChangeRequestProposal, preview['id'])
    if mutation == 'hash':
        body['expected_proposal_hash'] = '0' * 64
    elif mutation == 'legacy':
        row.parser_version = 'change-request-parser-v4'
    elif mutation == 'stale':
        row.status = 'stale'
    elif mutation == 'revision':
        db.query(EditorDocument).filter_by(job_id=job_id).one().revision += 1
    elif mutation == 'request':
        db.get(DeliveryChangeRequest, body['change_request_id']).comment = 'Un pedido diferente'
    elif mutation == 'audio':
        db.query(Job).filter_by(job_id=job_id).one().audio_revision = 8
    else:
        row.operations = [{**op, 'regeneration_supported': False} for op in row.operations]
    db.commit()
    response = client.post('/edit/' + job_id, headers=auth(admin_token), json=body)
    assert response.status_code == 409, response.text
    assert response.json()['detail']['code'] == 'proposal_preview_changed'
    assert calls == []
    db.expire_all()
    assert db.query(Job).filter_by(job_id=job_id).one().status == 'done'


def test_current_background_intent_is_accepted_once_and_replayed(client, admin_token, background_case):
    job_id, _, body, calls = background_case
    headers = {**auth(admin_token), 'Idempotency-Key': 'synthetic-background-intent'}
    first = client.post('/edit/' + job_id, headers=headers, json=body)
    assert first.status_code == 202, first.text
    again = client.post('/edit/' + job_id, headers=headers, json=body)
    assert again.status_code == 202 and again.json()['deduplicated'], again.text
    assert len(calls) == 1
    changed = client.post('/edit/' + job_id, headers=headers,
                          json={**body, 'background_hint': 'Otro fondo'})
    assert changed.status_code == 409
    assert len(calls) == 1


@pytest.mark.parametrize('mutation', ['audio', 'report'])
def test_edit_locked_reread_rejects_state_changed_during_storage_probe(
    client, admin_token, db, background_case, monkeypatch, mutation,
):
    """FOR UPDATE must refresh the Job already loaded by the preflight probe."""
    from copy import deepcopy
    import database
    from database import JobOutboxEvent

    job_id, _, background_body, calls = background_case
    job = db.query(Job).filter_by(job_id=job_id).one()
    job.input_r2_key = 'synthetic/input.wav'
    if mutation == 'report':
        # Use a nonzero revision: this specifically exercises preview-token
        # fencing, not the legacy revision-zero stale-report gate.
        job.segments_revision = 1
        doc = db.query(EditorDocument).filter_by(job_id=job_id).one()
        doc.revision = 1
        job.delivery_qc = {
            'status': 'COMPLETE', 'report_id': 'review-A', 'segments_revision': 1,
            'issues': [], 'repairs': {'actions': [{
                'action_id': 'safe-action', 'status': 'APPLIED', 'domain': 'text',
            }]},
        }
        body = {
            'edit_type': 'lyrics', 'editor_revision': 1,
            'segments': deepcopy(doc.current_segments),
            'delivery_qc_action_ids': ['safe-action'],
            'expected_delivery_qc_report_id': 'review-A',
        }
    else:
        body = background_body
    db.commit()
    db.rollback()
    mutated = []
    monkeypatch.setattr('main.storage.is_enabled', lambda: True)

    async def concurrent_probe(_function, _key, **_kwargs):
        if not mutated:
            with database.SessionLocal() as concurrent:
                current = concurrent.query(Job).filter_by(job_id=job_id).one()
                if mutation == 'audio':
                    current.audio_revision = int(current.audio_revision or 0) + 1
                else:
                    current.delivery_qc = {**current.delivery_qc, 'report_id': 'review-B'}
                concurrent.commit()
            mutated.append(True)
        return True

    monkeypatch.setattr('main._bounded_storage_probe', concurrent_probe)
    response = client.post('/edit/' + job_id, headers=auth(admin_token), json=body)
    assert mutated
    assert response.status_code == 409, response.text
    expected = 'proposal_preview_changed' if mutation == 'audio' else 'delivery_qc_preview_changed'
    assert response.json()['detail']['code'] == expected
    assert calls == []
    db.expire_all()
    persisted = db.query(Job).filter_by(job_id=job_id).one()
    assert persisted.status == 'done'
    if mutation == 'report':
        assert persisted.delivery_qc['report_id'] == 'review-B'
    assert db.query(JobOutboxEvent).filter_by(job_id=job_id, event_type='edit.enqueue').count() == 0


@pytest.mark.parametrize('entrypoint', ['edit', 'manual-render'])
def test_manual_edit_releases_local_connection_before_context_and_storage(
    client, admin_token, db, background_case, monkeypatch, entrypoint,
):
    import asyncio
    import database
    import main
    from sqlalchemy import event

    job_id, proposal, background_body, calls = background_case
    job = db.query(Job).filter_by(job_id=job_id).one()
    job.input_r2_key = 'synthetic/manual-input.wav'
    db.commit()
    db.rollback()
    body = {'edit_type': 'lyrics', 'editor_revision': proposal['base_revision'],
            'change_request_id': background_body['change_request_id']}
    locals_seen, portals_seen, reads, heads, db_calls = [], [], [], [], []
    original_overrides = dict(main.app.dependency_overrides)
    real_deliveries_factory = database.DeliveriesSessionLocal

    def local_dependency():
        session = database.SessionLocal()
        locals_seen.append(session)

        def statement(_state):
            # A blocked SELECT/FOR UPDATE must run in the worker pool, never
            # the loop responsible for responses and dependency cleanup.
            with pytest.raises(RuntimeError, match='no running event loop'):
                asyncio.get_running_loop()
            db_calls.append(True)

        event.listen(session, 'do_orm_execute', statement)
        try:
            yield session
        finally:
            session.rollback()
            session.close()

    def portal_factory():
        session = real_deliveries_factory()
        portals_seen.append(session)

        def begin(_session, _transaction, _connection):
            # Session construction itself checks out nothing. Test the moment
            # the external context actually acquires its pooled connection.
            assert locals_seen and all(not s.in_transaction() for s in locals_seen)
            reads.append(True)

        event.listen(session, 'after_begin', begin)
        return session

    def head(_key):
        assert locals_seen and all(not s.in_transaction() for s in locals_seen + portals_seen)
        with pytest.raises(RuntimeError, match='no running event loop'):
            asyncio.get_running_loop()
        heads.append(True)
        return True

    monkeypatch.setattr(database, 'DeliveriesSessionLocal', portal_factory)
    monkeypatch.setattr(main.storage, 'is_enabled', lambda: True)
    monkeypatch.setattr(main.storage, 'object_exists', head)
    main.app.dependency_overrides[main.get_db] = local_dependency
    try:
        headers = {**auth(admin_token), 'Idempotency-Key': 'manual-context-connection-boundary'}
        path = '/edit/' + job_id
        payload = body
        if entrypoint == 'manual-render':
            path = f"/admin/change-requests/{body['change_request_id']}/render"
            payload = {'editor_revision': body['editor_revision']}
        first = client.post(path, headers=headers, json=payload)
        assert first.status_code == 202, first.text
        assert reads and heads and db_calls and len(calls) == 1
        reads_after_first = len(reads)
        # /render checks the case at entry; the inner edit durable replay
        # must not acquire another context session after that check.
        second = client.post(path, headers=headers, json=payload)
        assert second.status_code == 202 and second.json()['deduplicated'], second.text
        assert len(reads) == reads_after_first + (entrypoint == 'manual-render')
        assert len(calls) == 1
    finally:
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(original_overrides)


@pytest.mark.parametrize('concurrency', [2, 8])
@pytest.mark.parametrize('entrypoint', ['edit', 'manual-render'])
def test_concurrent_manual_correction_renders_complete_with_one_outbox_effect(
    client, admin_token, db, background_case, monkeypatch, concurrency, entrypoint,
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from database import JobOutboxEvent

    if db.bind.dialect.name != 'postgresql':
        pytest.skip('Real pooled sessions and row locks require PostgreSQL')
    job_id, proposal, background_body, calls = background_case
    body = {'edit_type': 'lyrics', 'editor_revision': proposal['base_revision'],
            'change_request_id': background_body['change_request_id']}
    headers = {**auth(admin_token), 'Idempotency-Key': 'parallel-manual-context-render'}
    path = '/edit/' + job_id
    payload = body
    if entrypoint == 'manual-render':
        path = f"/admin/change-requests/{body['change_request_id']}/render"
        payload = {'editor_revision': body['editor_revision']}
    monkeypatch.setattr('main.storage.is_enabled', lambda: False)
    db.rollback()
    barrier = Barrier(concurrency, timeout=10)

    def submit(_):
        barrier.wait()
        return client.post(path, headers=headers, json=payload)

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        responses = list(pool.map(submit, range(concurrency)))
    assert all(response.status_code == 202 for response in responses), [
        (response.status_code, response.text) for response in responses
    ]
    assert len(calls) == 1
    db.expire_all()
    assert db.query(JobOutboxEvent).filter_by(job_id=job_id, event_type='edit.enqueue').count() == 1
