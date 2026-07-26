"""Tenant-scope regressions for the human approval endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from database import AuditLog, Job


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
    job_id = f"approval_scope_{uuid.uuid4().hex[:8]}"
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
    job_ids = [
        row[0]
        for row in db.query(Job.job_id)
        .filter(Job.job_id.like("approval_scope_%"))
        .all()
    ]
    if job_ids:
        db.query(Job).filter(Job.job_id.in_(job_ids)).delete(
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
