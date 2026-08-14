"""Editor drafts, versions, locks and workspace collaboration."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest

from auth import create_user, start_login_session
from database import AuditLog, EditorDocument, EditorVersion, Job, SessionLocal, engine
from editor import acquire_lock, approve_document, ensure_document, get_or_create_document, save_document
from tests.conftest import auth


def _users_and_job(tenant="editor_team"):
    db = SessionLocal()
    try:
        first = create_user(db, f"editor_a_{uuid.uuid4().hex[:6]}", "testpass12345", None, tenant_id=tenant)
        second = create_user(db, f"editor_b_{uuid.uuid4().hex[:6]}", "testpass12345", None, tenant_id=tenant)
        job_id = f"ed_{uuid.uuid4().hex[:9]}"
        db.add(Job(
            job_id=job_id, user_id=first.id, tenant_id=tenant,
            artist="Test Artist", song_title="Test Song", filename="test.wav",
            style="oscuro", status="transcribed_pending", current_step="editing",
            delivery_profile="youtube",
        ))
        db.commit()
        ensure_document(db, job_id, tenant, [
            {"start": 0, "end": 1, "text": "one"},
            {"start": 1, "end": 2, "text": "two"},
        ])
        db.commit()
        return first, second, job_id
    finally:
        db.close()


def _token_for(user):
    """Create a browser-compatible token backed by a live login session."""
    db = SessionLocal()
    try:
        return start_login_session(db, user)
    finally:
        db.close()


def test_editor_document_is_shared_by_tenant_and_conflicts_are_explicit(client):
    first, second, job_id = _users_and_job()
    token_a = _token_for(first)
    token_b = _token_for(second)

    loaded = client.get(f"/editor/{job_id}", headers=auth(token_b))
    assert loaded.status_code == 200
    assert loaded.json()["revision"] == 0
    assert loaded.json()["segments"][0]["text"] == "one"

    saved = client.patch(
        f"/editor/{job_id}",
        headers=auth(token_a),
        json={
            "base_revision": 0,
            "segments": [
                {"start": 0, "end": 1, "text": "ONE"},
                {"start": 1, "end": 2, "text": "two"},
            ],
            "checkpoint": "manual",
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["revision"] == 1

    conflict = client.patch(
        f"/editor/{job_id}",
        headers=auth(token_b),
        json={
            "base_revision": 0,
            "segments": [{"start": 0, "end": 1, "text": "stale"}],
        },
    )
    assert conflict.status_code == 409
    detail = conflict.json()["detail"]
    assert detail["detail"] == "editor_revision_conflict"
    assert detail["server_revision"] == 1
    assert detail["server_segments"][0]["text"] == "ONE"


def test_editor_versions_restore_and_lock_are_tenant_scoped(client):
    first, second, job_id = _users_and_job("editor_locks")
    token_a = _token_for(first)
    token_b = _token_for(second)

    lock_a = client.post(f"/editor/{job_id}/lock", headers=auth(token_a))
    assert lock_a.status_code == 200
    assert lock_a.json()["acquired"] is True

    lock_b = client.post(f"/editor/{job_id}/lock", headers=auth(token_b))
    assert lock_b.status_code == 200
    assert lock_b.json()["acquired"] is False
    assert lock_b.json()["user"]["id"] == first.id

    heartbeat = client.post(f"/editor/{job_id}/lock/heartbeat", headers=auth(token_a))
    assert heartbeat.status_code == 200
    assert heartbeat.json()["acquired"] is True

    saved = client.patch(
        f"/editor/{job_id}", headers=auth(token_a),
        json={"base_revision": 0, "segments": [{"start": 0, "end": 1, "text": "changed"}]},
    )
    assert saved.status_code == 200
    version_id = saved.json()["version_id"]

    versions = client.get(f"/editor/{job_id}/versions", headers=auth(token_b))
    assert versions.status_code == 200
    assert any(v["id"] == version_id for v in versions.json()["versions"])

    restored = client.post(
        f"/editor/{job_id}/restore", headers=auth(token_b),
        json={"version_id": versions.json()["versions"][-1]["id"], "base_revision": 1},
    )
    assert restored.status_code == 200
    assert restored.json()["revision"] == 2

    db = SessionLocal()
    try:
        document = db.query(EditorDocument).filter(EditorDocument.job_id == job_id).one()
        document.lock_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    finally:
        db.close()
    takeover = client.post(f"/editor/{job_id}/lock", headers=auth(token_b))
    assert takeover.status_code == 200
    assert takeover.json()["acquired"] is True
    assert client.delete(f"/editor/{job_id}/lock", headers=auth(token_a)).status_code == 409
    assert client.delete(f"/editor/{job_id}/lock", headers=auth(token_b)).status_code == 200


def test_editor_rejects_other_tenant_and_analytics_is_bounded(client):
    first, _, job_id = _users_and_job("editor_private")
    other = create_user(
        SessionLocal(), f"other_{uuid.uuid4().hex[:6]}", "testpass12345", None,
        tenant_id="another_team",
    )
    try:
        response = client.get(f"/editor/{job_id}", headers=auth(_token_for(other)))
        assert response.status_code == 404
    finally:
        db = SessionLocal()
        db.query(EditorDocument).filter(EditorDocument.job_id == job_id).delete()
        db.query(EditorVersion).filter(EditorVersion.job_id == job_id).delete()
        db.close()

    events = client.post(
        "/analytics/events", headers=auth(_token_for(first)),
        json={"events": [
            {"name": "editor_opened", "properties": {"line_count": 2}},
            {"name": "not_allowed", "properties": {"lyrics": "secret"}},
        ]},
    )
    assert events.status_code == 200
    assert events.json()["accepted"] == 1


def test_platform_admin_can_use_editor_for_cross_tenant_review(client):
    _, _, job_id = _users_and_job("editor_client_workspace")
    db = SessionLocal()
    try:
        admin = create_user(
            db, f"editor_admin_{uuid.uuid4().hex[:6]}", "testpass12345", None,
            role="admin", tenant_id="editor_platform_admin",
        )
        admin_id = admin.id
    finally:
        db.close()

    token = _token_for(admin)
    loaded = client.get(f"/editor/{job_id}", headers=auth(token))
    assert loaded.status_code == 200, loaded.text
    assert loaded.json()["segments"][0]["text"] == "one"

    saved = client.patch(
        f"/editor/{job_id}", headers=auth(token),
        json={
            "base_revision": 0,
            "segments": [{"start": 0, "end": 1, "text": "admin correction"}],
            "checkpoint": "manual",
        },
    )
    assert saved.status_code == 200, saved.text

    verify = SessionLocal()
    try:
        job = verify.query(Job).filter(Job.job_id == job_id).one()
        document = verify.query(EditorDocument).filter(EditorDocument.job_id == job_id).one()
        assert job.tenant_id == "editor_client_workspace"
        assert document.tenant_id == "editor_client_workspace"
        assert document.current_segments[0]["text"] == "admin correction"
        actions = {
            row.action for row in verify.query(AuditLog).filter(AuditLog.user_id == admin_id).all()
        }
        assert "admin.cross_tenant_access" in actions
    finally:
        verify.close()


def test_editor_preserves_metadata_draft_retry_and_version_details(client):
    first, _, job_id = _users_and_job("editor_metadata")
    token = _token_for(first)
    metadata_segments = [{
        "_id": "stable-row", "start": 0, "end": 1.25, "text": "hola",
        "words": [{"word": "hola", "start": 0, "end": 1}],
        "locked": True, "review": True,
        "pos": {"x": 0.25, "y": 0.7}, "scale": 1.2, "rot": -2,
        "future_field": {"kept": True},
    }]
    draft = client.patch(
        f"/editor/{job_id}", headers=auth(token),
        json={"base_revision": 0, "segments": metadata_segments, "checkpoint": "draft"},
    )
    assert draft.status_code == 200, draft.text
    assert draft.json()["revision"] == 1
    assert draft.json()["version_id"] is None

    # Lost-response retry: stale base but byte-equivalent content is a 200
    # no-op, never a false conflict and never another revision.
    retry = client.patch(
        f"/editor/{job_id}", headers=auth(token),
        json={"base_revision": 0, "segments": metadata_segments, "checkpoint": "draft"},
    )
    assert retry.status_code == 200
    assert retry.json()["revision"] == 1
    assert retry.json()["applied"] is False

    # If the delayed 5-second checkpoint raced the draft request, its stale
    # but identical payload still creates the durable version at revision 1.
    raced_checkpoint = client.patch(
        f"/editor/{job_id}", headers=auth(token),
        json={"base_revision": 0, "segments": metadata_segments, "checkpoint": "autosave"},
    )
    assert raced_checkpoint.status_code == 200
    assert raced_checkpoint.json()["applied"] is False
    assert raced_checkpoint.json()["version_id"]

    checkpoint = client.patch(
        f"/editor/{job_id}", headers=auth(token),
        json={"base_revision": 1, "segments": metadata_segments, "checkpoint": "manual"},
    )
    assert checkpoint.status_code == 200
    version_id = checkpoint.json()["version_id"]

    summaries = client.get(f"/editor/{job_id}/versions", headers=auth(token)).json()["versions"]
    assert all("segments" not in version for version in summaries)
    detail = client.get(f"/editor/{job_id}/versions/{version_id}", headers=auth(token))
    assert detail.status_code == 200
    assert detail.json()["segments"][0] == metadata_segments[0]


def test_stale_renderer_metadata_retry_is_not_a_collaboration_conflict(client):
    first, _, job_id = _users_and_job("editor_renderer_retry")
    token = _token_for(first)
    base = [{
        "_id": "row-1", "start": 0, "end": 1, "text": "hola",
        "words": [{"word": "hola", "start": 0, "end": 1}], "review": False,
    }]
    saved = client.patch(
        f"/editor/{job_id}", headers=auth(token),
        json={"base_revision": 0, "segments": base, "checkpoint": "draft"},
    )
    assert saved.status_code == 200
    assert saved.json()["revision"] == 1

    # A background/typography render refreshed IDs and word-review metadata
    # after the operator's request was sent. The stale retry is semantically
    # the same lyric snapshot and must not open the collaboration resolver.
    renderer_snapshot = [{
        "_id": "new-row-id", "segment_id": "row-1", "start": 0, "end": 1,
        "text": "hola", "words": [{"word": "hola", "start": 0.1, "end": 0.9}],
        "review": True,
    }]
    retry = client.patch(
        f"/editor/{job_id}", headers=auth(token),
        json={
            "base_revision": 0,
            "segments": renderer_snapshot,
            "checkpoint": "autosave",
        },
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["revision"] == 1
    assert retry.json()["applied"] is False


def test_conflict_resolution_preserves_both_and_rechecks_third_edit(client):
    first, second, job_id = _users_and_job("editor_conflict_resolution")
    token_a = _token_for(first)
    token_b = _token_for(second)
    team = [{"start": 0, "end": 1, "text": "team"}]
    local = [{"start": 0, "end": 1, "text": "local"}]
    third = [{"start": 0, "end": 1, "text": "third"}]
    assert client.patch(
        f"/editor/{job_id}", headers=auth(token_b),
        json={"base_revision": 0, "segments": team, "checkpoint": "manual"},
    ).status_code == 200

    resolved = client.post(
        f"/editor/{job_id}/conflicts/resolve", headers=auth(token_a),
        json={"strategy": "save_local_as_new", "server_revision": 1, "segments": local},
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["revision"] == 2
    assert resolved.json()["segments"] == local

    assert client.patch(
        f"/editor/{job_id}", headers=auth(token_b),
        json={"base_revision": 2, "segments": third, "checkpoint": "manual"},
    ).status_code == 200
    stale_dialog = client.post(
        f"/editor/{job_id}/conflicts/resolve", headers=auth(token_a),
        json={"strategy": "save_local_as_new", "server_revision": 2, "segments": local},
    )
    assert stale_dialog.status_code == 409
    assert stale_dialog.json()["detail"]["server_revision"] == 3

    db = SessionLocal()
    try:
        snapshots = db.query(EditorVersion).filter(EditorVersion.job_id == job_id).all()
        texts = {version.segments[0]["text"] for version in snapshots}
        assert {"team", "local", "third"}.issubset(texts)
    finally:
        db.close()


def test_historical_revision_is_used_for_lazy_document(client):
    db = SessionLocal()
    try:
        user = create_user(db, f"historical_{uuid.uuid4().hex[:6]}", "testpass12345", None, tenant_id="editor_history")
        job_id = f"ed_{uuid.uuid4().hex[:9]}"
        segments = [{"start": 2, "end": 3, "text": "historic", "_id": 99}]
        db.add(Job(
            job_id=job_id, user_id=user.id, tenant_id="editor_history",
            artist="History", filename="history.wav", style="oscuro",
            status="transcribed_pending", current_step="editing",
            delivery_profile="youtube", segments_json=segments, segments_revision=7,
        ))
        db.commit()
    finally:
        db.close()
    response = client.get(f"/editor/{job_id}", headers=auth(_token_for(user)))
    assert response.status_code == 200
    assert response.json()["revision"] == 7
    assert response.json()["segments"] == segments


def test_equal_revision_legacy_timing_anomaly_is_migrated_once():
    first, _, job_id = _users_and_job("editor_timing_repair")
    malformed = [
        {"_id": 9, "start": 45.1106, "end": 45.8752, "text": "uoo no no te hice daño,"},
        {"_id": 10, "start": 45.9252, "end": 46.5273, "text": "te alejaste de miSsi"},
        {"_id": 11, "start": 45.1606, "end": 46.9606, "text": "Las palabras se fueron al viento y no se."},
        {"_id": 22, "start": 114.766, "end": 115.967, "text": "¡Gracias!"},
        {"_id": 23, "start": 45.1106, "end": 45.8752, "text": "uoo no no te hice daño,"},
        {"_id": 24, "start": 45.9252, "end": 46.5273, "text": "te alejaste de mi"},
    ]
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.job_id == job_id).one()
        document = db.query(EditorDocument).filter(EditorDocument.job_id == job_id).one()
        job.segments_json = malformed
        document.current_segments = malformed
        db.commit()
        repaired = get_or_create_document(db, job_id, job.tenant_id, malformed)
        db.commit()
        assert [row["_id"] for row in repaired.current_segments] == [9, 10, 11, 22]
        assert repaired.current_segments[2]["start"] == 45.9752
        assert job.segments_revision == repaired.revision == 2
    finally:
        db.close()


def test_lazy_reconciliation_preserves_equal_revision_divergence():
    first, _, job_id = _users_and_job("editor_reconcile")
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.job_id == job_id).one()
        job.segments_json = [{"start": 0, "end": 1, "text": "deployed job"}]
        job.segments_revision = 0
        db.commit()

        document = get_or_create_document(db, job_id, job.tenant_id, job.segments_json)
        db.commit()
        assert document.revision == 2
        assert document.current_segments[0]["text"] == "deployed job"
        assert job.segments_revision == 2
        versions = db.query(EditorVersion).filter(EditorVersion.job_id == job_id).order_by(EditorVersion.revision).all()
        assert versions[-2].segments[0]["text"] == "one"
        assert versions[-1].segments[0]["text"] == "deployed job"
        assert versions[-1].reason == "migration"
    finally:
        db.close()


def test_lazy_reconciliation_skips_an_orphan_conflicting_revision():
    first, _, job_id = _users_and_job("editor_reconcile_orphan")
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.job_id == job_id).one()
        document = db.query(EditorDocument).filter(EditorDocument.job_id == job_id).one()
        document.current_segments = [{"start": 0, "end": 1, "text": "document current"}]
        document.revision = 2
        job.segments_json = [{"start": 0, "end": 1, "text": "legacy old"}]
        job.segments_revision = 1
        db.add(EditorVersion(
            id=str(uuid.uuid4()), job_id=job_id, tenant_id=job.tenant_id,
            revision=2, segments=[{"start": 0, "end": 1, "text": "orphan conflict"}],
            created_by=first.id, reason="migration",
        ))
        db.commit()

        reconciled = get_or_create_document(db, job_id, job.tenant_id, job.segments_json)
        db.commit()
        assert reconciled.revision == 3
        assert reconciled.current_segments[0]["text"] == "document current"
        assert job.segments_revision == 3
        assert db.query(EditorVersion).filter(
            EditorVersion.job_id == job_id, EditorVersion.revision == 3,
        ).one().segments[0]["text"] == "document current"
    finally:
        db.close()


def test_analytics_rejects_cross_tenant_nested_and_oversized_batches(client):
    first, _, job_id = _users_and_job("editor_analytics")
    other_first, _, other_job_id = _users_and_job("editor_analytics_other")
    token = _token_for(first)
    response = client.post(
        "/analytics/events", headers=auth(token),
        json={"events": [
            {"name": "editor_opened", "job_id": job_id, "properties": {"line_count": 2}},
            {"name": "editor_opened", "job_id": other_job_id, "properties": {"line_count": 2}},
            {"name": "editor_opened", "properties": {"line_count": 2, "lyrics": {"text": "secret"}}},
            {"name": "unknown", "properties": {}},
        ]},
    )
    assert response.status_code == 200
    assert response.json() == {"accepted": 1, "rejected": 3}
    too_many = client.post(
        "/analytics/events", headers=auth(token),
        json={"events": [{"name": "editor_opened", "properties": {}} for _ in range(51)]},
    )
    assert too_many.status_code == 422
    assert other_first.id != first.id


def test_transaction_rollback_never_leaves_partial_editor_state():
    first, _, job_id = _users_and_job("editor_rollback")
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.job_id == job_id).one()
        document = db.query(EditorDocument).filter(EditorDocument.job_id == job_id).one()
        save_document(
            db, job, document, first.id, 0,
            [{"start": 0, "end": 1, "text": "must rollback"}], "manual",
        )
        db.rollback()
    finally:
        db.close()
    verify = SessionLocal()
    try:
        job = verify.query(Job).filter(Job.job_id == job_id).one()
        document = verify.query(EditorDocument).filter(EditorDocument.job_id == job_id).one()
        assert document.revision == 0
        assert job.segments_revision == 0
        assert document.current_segments[0]["text"] == "one"
        assert job.segments_json[0]["text"] == "one"
        assert verify.query(EditorVersion).filter(EditorVersion.job_id == job_id).count() == 1
    finally:
        verify.close()


def test_approval_requires_the_current_exact_snapshot():
    first, _, job_id = _users_and_job("editor_approval")
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.job_id == job_id).one()
        document = db.query(EditorDocument).filter(EditorDocument.job_id == job_id).one()
        document, version, _ = save_document(
            db, job, document, first.id, 0,
            [{"start": 0, "end": 1, "text": "approved exact"}], "manual",
        )
        db.commit()
        approved_document, approved_version = approve_document(
            db, job, first.id, editor_revision=1, editor_version_id=version.id,
        )
        db.commit()
        assert approved_document.revision == 1
        assert approved_version.is_approved is True
        assert approved_version.segments[0]["text"] == "approved exact"

        document = db.query(EditorDocument).filter(EditorDocument.job_id == job_id).one()
        save_document(
            db, job, document, first.id, 1,
            [{"start": 0, "end": 1, "text": "remote intermediate"}], "manual",
        )
        db.commit()
        with pytest.raises(RuntimeError, match="editor_revision_conflict"):
            approve_document(db, job, first.id, editor_version_id=version.id)
        db.rollback()
    finally:
        db.close()


def test_approval_allows_renderer_metadata_revision_when_lyrics_are_unchanged():
    first, _, job_id = _users_and_job("editor_approval_metadata")
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.job_id == job_id).one()
        document = db.query(EditorDocument).filter(EditorDocument.job_id == job_id).one()
        document, version, _ = save_document(
            db, job, document, first.id, 0,
            [
                {"start": 0, "end": 1, "text": "one", "words": [{"start": 0}]},
                {"start": 1, "end": 2, "text": "two"},
            ], "manual",
        )
        db.commit()

        document = db.query(EditorDocument).filter(EditorDocument.job_id == job_id).one()
        save_document(
            db, job, document, first.id, 1,
            [
                {"start": 0, "end": 1, "text": "one", "words": [{"start": 0.2}], "review": True},
                {"start": 1, "end": 2, "text": "two"},
            ], "migration",
        )
        db.commit()

        approved_document, approved_version = approve_document(
            db, job, first.id, editor_revision=1, editor_version_id=version.id,
        )
        db.commit()
        assert approved_document.revision == 2
        assert approved_version.is_approved is True
        assert approved_version.segments[0]["text"] == "one"
    finally:
        db.close()


def test_retention_keeps_fifty_drafts_and_all_approved_snapshots():
    first, _, job_id = _users_and_job("editor_retention")
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.job_id == job_id).one()
        document = db.query(EditorDocument).filter(EditorDocument.job_id == job_id).one()
        document, approved, _ = save_document(
            db, job, document, first.id, 0,
            [{"start": 0, "end": 1, "text": "approved"}], "approve",
        )
        approved_id = approved.id
        for index in range(55):
            document, _, _ = save_document(
                db, job, document, first.id, document.revision,
                [{"start": 0, "end": 1, "text": f"draft-{index}"}], "manual",
            )
        db.commit()
        drafts = db.query(EditorVersion).filter(
            EditorVersion.job_id == job_id,
            EditorVersion.is_approved.is_(False),
        ).all()
        assert len(drafts) == 50
        assert db.query(EditorVersion).filter(EditorVersion.id == approved_id).one().is_approved is True
    finally:
        db.close()


@pytest.mark.skipif(engine.dialect.name != "postgresql", reason="requires PostgreSQL row locks")
def test_postgres_two_simultaneous_patches_exactly_one_wins():
    first, _, job_id = _users_and_job("editor_pg_patch")
    barrier = Barrier(2)

    def writer(text):
        db = SessionLocal()
        try:
            job = db.query(Job).filter(Job.job_id == job_id).one()
            document = db.query(EditorDocument).filter(EditorDocument.job_id == job_id).one()
            barrier.wait(timeout=5)
            try:
                save_document(db, job, document, first.id, 0, [{"start": 0, "end": 1, "text": text}], "manual")
                db.commit()
                return "won"
            except RuntimeError:
                db.rollback()
                return "conflict"
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(writer, ["alpha", "beta"]))
    assert sorted(outcomes) == ["conflict", "won"]


@pytest.mark.skipif(engine.dialect.name != "postgresql", reason="requires PostgreSQL row locks")
def test_postgres_two_simultaneous_locks_exactly_one_gets_lease():
    first, second, job_id = _users_and_job("editor_pg_lock")
    barrier = Barrier(2)

    def locker(user_id):
        db = SessionLocal()
        try:
            document = db.query(EditorDocument).filter(EditorDocument.job_id == job_id).one()
            barrier.wait(timeout=5)
            result = acquire_lock(db, document, user_id)
            db.commit()
            return result["acquired"]
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(locker, [first.id, second.id]))
    assert sorted(outcomes) == [False, True]
