"""Tenant isolation guarantees.

Two tenants must NOT see, download, modify, or delete each other's
jobs. The product depends on this for multi-label B2B operation
(e.g. one Universal team should not see Sony's jobs running on the
same DB).

Scenarios covered:
  - GET /jobs (list) — each tenant sees only its own.
  - GET /status/{job_id} — 404 across tenants.
  - DELETE /jobs/{job_id} — 404 across tenants.
  - POST /jobs/bulk-delete — silently skips other-tenant ids.
  - GET /provenance/{job_id} — 404 across tenants.

Plus the multi-user-same-tenant case the operator cares about for
UMG: two users that share `tenant_id` DO see the same job list.
"""

import uuid

from database import SessionLocal, Job
from auth import create_user, create_token
from tests.conftest import auth


def _seed_job(db, tenant_id: str, user_id: int, status: str = "done") -> str:
    """Drop a single Job row directly for the given tenant/user."""
    job_id = f"isol_{uuid.uuid4().hex[:8]}"
    db.add(Job(
        job_id=job_id,
        user_id=user_id,
        tenant_id=tenant_id,
        artist="Test Artist",
        filename="test.mp3",
        style="oscuro",
        status=status,
        current_step="done",
        progress=100,
        delivery_profile="youtube",
    ))
    db.commit()
    return job_id


def _make_user(db, tenant_id: str, username_prefix: str):
    user = create_user(
        db,
        username=f"{username_prefix}_{uuid.uuid4().hex[:6]}",
        password="testpass12345",
        email=None,
        tenant_id=tenant_id,
    )
    token = create_token(user)
    return user, token


def test_two_tenants_do_not_see_each_others_jobs(client):
    db = SessionLocal()
    try:
        user_a, token_a = _make_user(db, "tenant_alpha", "alpha")
        user_b, token_b = _make_user(db, "tenant_beta",  "beta")
        job_a = _seed_job(db, "tenant_alpha", user_a.id)
        job_b = _seed_job(db, "tenant_beta",  user_b.id)
    finally:
        db.close()

    # Each list endpoint shows only the caller's tenant jobs.
    list_a = client.get("/jobs", headers=auth(token_a))
    list_b = client.get("/jobs", headers=auth(token_b))
    assert list_a.status_code == 200
    assert list_b.status_code == 200
    ids_a = {j["job_id"] for j in list_a.json()}
    ids_b = {j["job_id"] for j in list_b.json()}
    assert job_a in ids_a
    assert job_b not in ids_a
    assert job_b in ids_b
    assert job_a not in ids_b


def test_status_404_across_tenants(client):
    db = SessionLocal()
    try:
        user_a, _ = _make_user(db, "tenant_gamma", "gamma")
        _, token_b = _make_user(db, "tenant_delta", "delta")
        job_a = _seed_job(db, "tenant_gamma", user_a.id)
    finally:
        db.close()

    res = client.get(f"/status/{job_a}", headers=auth(token_b))
    assert res.status_code in (403, 404), (
        f"expected 403/404 across tenants, got {res.status_code}"
    )


def test_delete_404_across_tenants(client):
    db = SessionLocal()
    try:
        user_a, _ = _make_user(db, "tenant_epsilon", "epsilon")
        _, token_b = _make_user(db, "tenant_zeta",   "zeta")
        job_a = _seed_job(db, "tenant_epsilon", user_a.id, status="error")
    finally:
        db.close()

    res = client.delete(f"/jobs/{job_a}", headers=auth(token_b))
    assert res.status_code in (403, 404), (
        f"expected 403/404 across tenants, got {res.status_code}"
    )

    # And the job must still exist for the rightful tenant.
    db = SessionLocal()
    try:
        assert db.query(Job).filter(Job.job_id == job_a).first() is not None
    finally:
        db.close()


def test_bulk_delete_skips_other_tenants(client):
    db = SessionLocal()
    try:
        user_a, _ = _make_user(db, "tenant_eta",   "eta")
        user_b, token_b = _make_user(db, "tenant_theta", "theta")
        job_a = _seed_job(db, "tenant_eta",   user_a.id, status="error")
        job_b = _seed_job(db, "tenant_theta", user_b.id, status="error")
    finally:
        db.close()

    res = client.post(
        "/jobs/bulk-delete",
        headers=auth(token_b),
        json={"job_ids": [job_a, job_b]},
    )
    assert res.status_code == 200
    body = res.json()
    deleted = set(body.get("deleted") or [])
    assert job_b in deleted, "tenant_theta's own job should be deleted"
    assert job_a not in deleted, "tenant_theta must NOT delete tenant_eta's job"

    db = SessionLocal()
    try:
        # tenant_eta's job survives.
        assert db.query(Job).filter(Job.job_id == job_a).first() is not None
    finally:
        db.close()


def test_provenance_404_across_tenants(client):
    db = SessionLocal()
    try:
        user_a, _ = _make_user(db, "tenant_iota",  "iota")
        _, token_b = _make_user(db, "tenant_kappa", "kappa")
        job_a = _seed_job(db, "tenant_iota", user_a.id)
    finally:
        db.close()

    res = client.get(f"/provenance/{job_a}", headers=auth(token_b))
    assert res.status_code in (403, 404), (
        f"expected 403/404 across tenants on /provenance, got {res.status_code}"
    )


def test_two_users_same_tenant_share_jobs(client):
    """The UMG case: 3 operators in the same workspace see each other's
    jobs. Tested with 2 users to keep the fixture small."""
    shared = "tenant_umg_test"
    db = SessionLocal()
    try:
        user_x, token_x = _make_user(db, shared, "umgx")
        _,      token_y = _make_user(db, shared, "umgy")
        job_x = _seed_job(db, shared, user_x.id)
    finally:
        db.close()

    list_y = client.get("/jobs", headers=auth(token_y))
    assert list_y.status_code == 200
    ids_y = {j["job_id"] for j in list_y.json()}
    assert job_x in ids_y, "teammate in same tenant should see the job"

    status_y = client.get(f"/status/{job_x}", headers=auth(token_y))
    assert status_y.status_code == 200, (
        "teammate in same tenant should be able to read status"
    )
