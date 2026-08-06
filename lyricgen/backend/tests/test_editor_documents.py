"""Editor drafts, versions, locks and workspace collaboration."""

import uuid

from auth import create_user, start_login_session
from database import EditorDocument, EditorVersion, Job, SessionLocal
from editor import ensure_document
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

    assert client.delete(f"/editor/{job_id}/lock", headers=auth(token_a)).status_code == 200


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
