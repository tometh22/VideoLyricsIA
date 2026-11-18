"""Tenant-scope regressions for the human approval endpoints."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from database import AuditLog, BatchCampaign, Job


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _register(client, prefix: str) -> tuple[str, dict]:
    username = f"{prefix}_{uuid.uuid4().hex[:8]}"
    response = client.post("/auth/register", json={
        "username": username,
        "password": "testpass12345",
        "email": f"{username}@test.com",
    })
    assert response.status_code == 200, response.text
    token = response.json()["token"]
    me = client.get("/auth/me", headers=_auth(token)).json()
    return token, me


def _seed_pending_review(db, owner: dict) -> str:
    # Job.job_id is VARCHAR(12) in Postgres. SQLite does not enforce the
    # declared length, so keep this fixture explicitly production-shaped.
    job_id = f"aps_{uuid.uuid4().hex[:8]}"
    db.add(Job(
        job_id=job_id,
        user_id=owner["id"],
        tenant_id=owner["tenant_id"],
        filename="approval-scope.wav",
        artist="Scope Artist",
        song_title="Scope Song",
        status="pending_review",
        progress=100,
        current_step="thumbnail",
        created_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
    ))
    db.commit()
    return job_id


@pytest.fixture(autouse=True)
def _cleanup(db):
    yield
    # Recover the shared fixture session even if setup/assertion failed during
    # a flush; otherwise teardown itself raises PendingRollbackError.
    db.rollback()
    job_ids = [
        row[0]
        for row in db.query(Job.job_id)
        .filter(Job.job_id.like("aps_%") | Job.job_id.like("aq%"))
        .all()
    ]
    if job_ids:
        db.query(Job).filter(Job.job_id.in_(job_ids)).delete(
            synchronize_session=False,
        )
    db.query(BatchCampaign).filter(BatchCampaign.id == "camp_over_01").delete(
        synchronize_session=False,
    )
    db.query(AuditLog).filter(AuditLog.action.in_([
        "job.approve",
        "job.reject",
        "admin.cross_tenant_access",
    ])).delete(synchronize_session=False)
    db.commit()


def test_admin_can_approve_cross_tenant_job(
    client, admin_token, admin_user_id, db,
):
    _, owner = _register(client, "approval_owner")
    job_id = _seed_pending_review(db, owner)

    response = client.post(
        f"/approve/{job_id}",
        headers=_auth(admin_token),
        json={"notes": "Revisado por soporte"},
    )

    assert response.status_code == 200, response.text
    db.expire_all()
    job = db.query(Job).filter(Job.job_id == job_id).one()
    assert job.status == "done"
    assert job.approved_by == admin_user_id

    approval_log = (
        db.query(AuditLog)
        .filter(AuditLog.action == "job.approve")
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert approval_log.detail["tenant_id"] == owner["tenant_id"]
    assert approval_log.detail["owner_user_id"] == owner["id"]
    assert approval_log.detail["cross_tenant_admin"] is True

    access_log = (
        db.query(AuditLog)
        .filter(AuditLog.action == "admin.cross_tenant_access")
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert access_log.detail["job_id"] == job_id
    assert access_log.detail["kind"] == "approve_job"


def test_umg_approval_requires_signed_current_video_review(client, db):
    from delivery_qc_runtime import (
        MANDATORY_REVIEW_CHECKS, delivery_qc_source_fingerprint,
        delivery_qc_visual_fingerprint, segments_hash,
    )

    owner_token, owner = _register(client, "approval_umg_review")
    job_id = _seed_pending_review(db, owner)
    job = db.query(Job).filter(Job.job_id == job_id).one()
    job.delivery_profile = "umg"
    job.umg_spec = {"frame_size": "HD", "fps": 29.97}
    now = datetime.now(timezone.utc).isoformat()
    job.delivery_qc = {
        "status": "COMPLETE", "mode": "enforce", "generated_at": now,
        "segments_revision": int(job.segments_revision or 0),
        "segments_hash": segments_hash(job.segments_json or []),
        "delivery_spec": dict(job.umg_spec),
        "source_fingerprint": delivery_qc_source_fingerprint(job),
        "visual_fingerprint": delivery_qc_visual_fingerprint(job),
        "render_identity": {"edit_count": int(job.edit_count or 0)},
        "issues": [{
            "issue_id": f"manual-{code}", "code": code,
            "status": "OPEN", "severity": "FAIL", "result_status": "REVIEW",
            "manual_verification_required": True,
        } for code, _summary, _description in MANDATORY_REVIEW_CHECKS],
    }
    db.commit()

    response = client.post(
        f"/approve/{job_id}", headers=_auth(owner_token), json={"notes": ""},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "delivery_qc_blocked"
    assert response.json()["detail"]["delivery_qc"]["reason"] == "manual_review_required"
    db.expire_all()
    assert db.query(Job).filter(Job.job_id == job_id).one().status == "pending_review"


def test_status_exposes_expiring_campaign_bypass_for_stale_preflight(
    client, db, monkeypatch,
):
    owner_token, owner = _register(client, "approval_umg_bypass_status")
    campaign_id = "camp_over_01"
    db.add(BatchCampaign(
        id=campaign_id,
        tenant_id=owner["tenant_id"],
        created_by=owner["id"],
        name="Stale preflight bypass fixture",
    ))
    db.flush()
    job_id = _seed_pending_review(db, owner)
    job = db.query(Job).filter(Job.job_id == job_id).one()
    job.delivery_profile = "umg"
    job.campaign_id = campaign_id
    job.delivery_qc = {
        "status": "STALE",
        "mode": "enforce",
        "approval": {"blocked": True, "can_approve": False, "reason": "fresh_preflight_required"},
        "issues": [{"issue_id": "old-cut", "status": "OPEN", "summary": "Hallazgo de otro corte"}],
    }
    db.commit()
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("DELIVERY_QC_UMG_STAGING_REVIEW_BYPASS", "1")
    monkeypatch.setenv("DELIVERY_QC_UMG_STAGING_REVIEW_BYPASS_CAMPAIGN_IDS", campaign_id)
    monkeypatch.setenv("DELIVERY_QC_UMG_STAGING_REVIEW_BYPASS_UNTIL_UTC", "2099-01-01T00:00:00Z")
    monkeypatch.setenv("DELIVERY_QC_UMG_STAGING_PREFLIGHT_BYPASS", "1")

    response = client.get(f"/status/{job_id}", headers=_auth(owner_token))

    assert response.status_code == 200, response.text
    report = response.json()["delivery_qc"]
    assert report["status"] == "BYPASSED"
    assert report["approval"]["can_approve"] is True
    assert report["approval"]["blocked"] is False
    assert report["approval"]["reason"] == "staging_preflight_bypass"
    assert report["issues"] == []  # findings from the replaced cut are never shown as current


def test_admin_override_can_approve_campaign_qc_blocker_with_audit(
    client, admin_token, admin_user_id, db,
):
    _, owner = _register(client, "approval_override_owner")
    job_id = _seed_pending_review(db, owner)
    campaign_id = "camp_over_01"
    db.add(BatchCampaign(
        id=campaign_id,
        tenant_id=owner["tenant_id"],
        created_by=owner["id"],
        name="Approval override fixture",
    ))
    db.flush()
    job = db.query(Job).filter(Job.job_id == job_id).one()
    job.campaign_id = campaign_id
    job.workload_class = "batch"
    job.delivery_qc = {
        "status": "COMPLETE",
        "issues": [{
            "issue_id": "title-mismatch",
            "code": "UMG_TITLE_METADATA",
            "severity": "FAIL",
            "status": "OPEN",
            "blocking": True,
        }],
    }
    db.commit()

    response = client.post(
        f"/approve/{job_id}",
        headers=_auth(admin_token),
        json={
            "notes": "Urgencia de campaña Chile",
            "admin_override": True,
            "override_reason": "Autorizado por Tomi para liberar campaña Chile y enviar a UMG Chile",
        },
    )

    assert response.status_code == 200, response.text
    db.expire_all()
    assert db.query(Job).filter(Job.job_id == job_id).one().status == "done"
    log = (
        db.query(AuditLog)
        .filter(AuditLog.action == "job.approve")
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert log.detail["admin_override"] is True
    assert "campaña Chile" in log.detail["override_reason"]
    assert log.detail["cross_tenant_admin"] is True


def test_regular_user_cannot_approve_other_tenant_job(client, db):
    _, owner = _register(client, "approval_owner")
    attacker_token, _ = _register(client, "approval_other")
    job_id = _seed_pending_review(db, owner)

    response = client.post(
        f"/approve/{job_id}",
        headers=_auth(attacker_token),
        json={"notes": ""},
    )

    assert response.status_code == 404
    db.expire_all()
    job = db.query(Job).filter(Job.job_id == job_id).one()
    assert job.status == "pending_review"


def test_approval_rechecks_owner_quota(client, db):
    owner_token, owner = _register(client, "approval_quota")
    job_id = _seed_pending_review(db, owner)
    now = datetime.now(timezone.utc)
    for index in range(5):
        db.add(Job(
            job_id=f"aq{index}_{uuid.uuid4().hex[:8]}",
            user_id=owner["id"],
            tenant_id=owner["tenant_id"],
            filename=f"approved-{index}.wav",
            artist="Quota Artist",
            song_title=f"Approved {index}",
            status="done",
            approved_by=owner["id"],
            approved_at=now,
        ))
    db.commit()

    response = client.post(
        f"/approve/{job_id}", headers=_auth(owner_token), json={"notes": ""},
    )

    assert response.status_code == 402, response.text
    db.expire_all()
    assert db.query(Job).filter(Job.job_id == job_id).one().status == "pending_review"


@pytest.mark.postgres
def test_concurrent_approvals_cannot_cross_monthly_quota(client, db):
    """The tenant advisory lock serializes count+approval at the limit."""
    owner_token, owner = _register(client, "approval_race")
    now = datetime.now(timezone.utc)
    for index in range(4):
        db.add(Job(
            job_id=f"aq{index}_{uuid.uuid4().hex[:8]}",
            user_id=owner["id"], tenant_id=owner["tenant_id"],
            filename=f"race-approved-{index}.wav", artist="Quota Artist",
            status="done", approved_by=owner["id"], approved_at=now,
        ))
    first = _seed_pending_review(db, owner)
    second = _seed_pending_review(db, owner)

    def approve(job_id):
        return client.post(
            f"/approve/{job_id}", headers=_auth(owner_token), json={"notes": ""},
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = sorted(pool.map(approve, (first, second)))
    assert statuses == [200, 402]


def test_admin_can_reject_cross_tenant_job(
    client, admin_token, admin_user_id, db,
):
    _, owner = _register(client, "rejection_owner")
    job_id = _seed_pending_review(db, owner)

    response = client.post(
        f"/reject/{job_id}",
        headers=_auth(admin_token),
        json={"notes": "Necesita cambios"},
    )

    assert response.status_code == 200, response.text
    db.expire_all()
    job = db.query(Job).filter(Job.job_id == job_id).one()
    assert job.status == "rejected"
    assert job.approved_by == admin_user_id

    access_log = (
        db.query(AuditLog)
        .filter(AuditLog.action == "admin.cross_tenant_access")
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert access_log.detail["job_id"] == job_id
    assert access_log.detail["kind"] == "reject_job"
