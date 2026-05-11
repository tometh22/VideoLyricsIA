"""POST /jobs/{job_id}/force-fail endpoint tests.

User-facing escape hatch: when a job's progress bar has been frozen for
2× the step's expected duration, ProgressPanel surfaces a banner with a
"Marcar como error" button → POST /jobs/{job_id}/force-fail. This file
pins the safety properties: only owner can fail (unless admin), only
in-flight states can be flagged, idempotent, audit-logged.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from database import Job, User, AuditLog


def _decode_user(client, token: str):
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    return me.json()


def _seed_job(db, *, owner_id: int, tenant_id: str, status: str = "processing"):
    jid = f"ff_{uuid.uuid4().hex[:6]}"
    db.add(Job(
        job_id=jid,
        user_id=owner_id,
        tenant_id=tenant_id,
        artist="Test",
        filename="x.mp3",
        style="oscuro",
        status=status,
        current_step="video",
        progress=40,
        delivery_profile="youtube",
        created_at=datetime.now(timezone.utc),
    ))
    db.commit()
    return jid


def _cleanup(db, prefix="ff_"):
    db.query(Job).filter(Job.job_id.like(f"{prefix}%")).delete(synchronize_session=False)
    db.query(AuditLog).filter(AuditLog.action == "job.force_fail").delete(synchronize_session=False)
    db.commit()


def test_owner_can_force_fail_their_processing_job(client, user_token, db):
    _cleanup(db)
    me = _decode_user(client, user_token)
    jid = _seed_job(db, owner_id=me["id"], tenant_id=me["tenant_id"])
    r = client.post(
        f"/jobs/{jid}/force-fail",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["status"] == "error"
    assert body["previous"]["status"] == "processing"
    db.expire_all()
    row = db.query(Job).filter(Job.job_id == jid).first()
    assert row.status == "error"
    assert "manualmente" in (row.error or "").lower()
    assert "reintentar" in (row.error or "").lower()
    assert row.completed_at is not None
    _cleanup(db)


def test_force_fail_records_audit_log_with_previous_state(client, user_token, db):
    _cleanup(db)
    me = _decode_user(client, user_token)
    jid = _seed_job(db, owner_id=me["id"], tenant_id=me["tenant_id"])
    client.post(
        f"/jobs/{jid}/force-fail",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    log = (
        db.query(AuditLog)
        .filter(AuditLog.action == "job.force_fail")
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert log is not None
    assert log.detail["job_id"] == jid
    assert log.detail["status"] == "processing"
    assert log.detail["current_step"] == "video"
    assert log.detail["progress"] == 40
    _cleanup(db)


def test_force_fail_is_idempotent(client, user_token, db):
    """Second click on the same job is a 200 no-op, not a 4xx."""
    _cleanup(db)
    me = _decode_user(client, user_token)
    jid = _seed_job(db, owner_id=me["id"], tenant_id=me["tenant_id"])
    hdrs = {"Authorization": f"Bearer {user_token}"}
    r1 = client.post(f"/jobs/{jid}/force-fail", headers=hdrs)
    assert r1.status_code == 200
    r2 = client.post(f"/jobs/{jid}/force-fail", headers=hdrs)
    assert r2.status_code == 200
    assert r2.json().get("no_op") is True
    _cleanup(db)


def test_force_fail_rejects_terminal_states(client, user_token, db):
    """`done` / `pending_review` are not in-flight — must 400."""
    _cleanup(db)
    me = _decode_user(client, user_token)
    hdrs = {"Authorization": f"Bearer {user_token}"}
    for term in ("done", "pending_review", "rejected"):
        jid = _seed_job(db, owner_id=me["id"], tenant_id=me["tenant_id"], status=term)
        r = client.post(f"/jobs/{jid}/force-fail", headers=hdrs)
        assert r.status_code == 400, f"status={term} should reject, got {r.status_code}"
        assert "cannot be force-failed" in r.text.lower()
    _cleanup(db)


def test_force_fail_404_for_other_tenants_job(client, user_token, db):
    """A non-admin user can't fail jobs from another tenant."""
    _cleanup(db)
    me = _decode_user(client, user_token)
    # Synthetic foreign tenant — the row points at the current user's
    # id (FK constraint) but a different tenant_id, so the endpoint's
    # tenant filter rejects it.
    jid = _seed_job(db, owner_id=me["id"], tenant_id="other_tenant_xx")
    r = client.post(
        f"/jobs/{jid}/force-fail",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert r.status_code in (403, 404), (
        f"non-admin should NOT fail a foreign-tenant job, got {r.status_code}: {r.text}"
    )
    _cleanup(db)


def test_force_fail_404_for_unknown_job(client, user_token, db):
    _cleanup(db)
    r = client.post(
        "/jobs/does_not_exist_xx/force-fail",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert r.status_code == 404


def test_force_fail_accepts_queued_status(client, user_token, db):
    """`queued` is in-flight from RQ's perspective (waiting for a
    worker to claim it). Must be force-failable so the user can give
    up on a never-started job too."""
    _cleanup(db)
    me = _decode_user(client, user_token)
    jid = _seed_job(
        db, owner_id=me["id"], tenant_id=me["tenant_id"], status="queued",
    )
    r = client.post(
        f"/jobs/{jid}/force-fail",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["previous"]["status"] == "queued"
    _cleanup(db)


def test_admin_can_force_fail_any_tenants_job(client, admin_token, user_token, db):
    """Admin's tenant filter is bypassed — they can fail jobs from
    any tenant. Models an operator forcing a stuck render off the
    queue without needing the tenant's credentials."""
    _cleanup(db)
    me = _decode_user(client, user_token)
    jid = _seed_job(db, owner_id=me["id"], tenant_id=me["tenant_id"])
    r = client.post(
        f"/jobs/{jid}/force-fail",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200, r.text
    _cleanup(db)
