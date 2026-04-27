"""Tests for per-user concurrent-jobs cap (batch-size limit).

The cap defines "max jobs in flight per tenant." It enforces "one batch at a
time": once your current batch's jobs all finish processing, you can start a
new batch. Default is DEFAULT_MAX_CONCURRENT_JOBS (10). Pending review and
terminal states don't count.
"""

import hashlib

import pytest
from fastapi import HTTPException

from main import _enforce_concurrent_jobs_cap, DEFAULT_MAX_CONCURRENT_JOBS


def _seed_processing_jobs(db, *, tenant_id: str, count: int, status: str = "processing"):
    """Seed N jobs in the given status for a tenant. Uses flush() so the
    conftest db fixture's rollback cleans up after each test."""
    from database import Job

    prefix = hashlib.sha1(f"cj-{tenant_id}-{status}".encode()).hexdigest()[:6]
    for i in range(count):
        db.add(Job(
            job_id=f"{prefix}{i:05d}"[:12],
            user_id=1,
            tenant_id=tenant_id,
            artist="Test",
            filename="x.mp3",
            status=status,
        ))
    db.flush()


def test_default_cap_is_10():
    """Sanity check on the suggested default — matches what's in the design doc."""
    assert DEFAULT_MAX_CONCURRENT_JOBS == 10


def test_under_cap_does_not_raise(db):
    _seed_processing_jobs(db, tenant_id="cj-under", count=5)
    user = {"id": 999, "tenant_id": "cj-under"}
    _enforce_concurrent_jobs_cap(db, user)  # should not raise


def test_at_cap_raises_429(db):
    _seed_processing_jobs(db, tenant_id="cj-exact", count=DEFAULT_MAX_CONCURRENT_JOBS)
    user = {"id": 999, "tenant_id": "cj-exact"}
    with pytest.raises(HTTPException) as exc:
        _enforce_concurrent_jobs_cap(db, user)
    assert exc.value.status_code == 429
    assert "batch limit" in exc.value.detail.lower()
    assert "10" in exc.value.detail


def test_over_cap_raises_429(db):
    _seed_processing_jobs(db, tenant_id="cj-over", count=DEFAULT_MAX_CONCURRENT_JOBS + 3)
    user = {"id": 999, "tenant_id": "cj-over"}
    with pytest.raises(HTTPException) as exc:
        _enforce_concurrent_jobs_cap(db, user)
    assert exc.value.status_code == 429


def test_pending_review_jobs_dont_count_toward_cap(db):
    """A backlog of unreviewed videos should not block new uploads — the
    pipeline isn't using resources on those, the user is."""
    _seed_processing_jobs(db, tenant_id="cj-pr",
                          count=DEFAULT_MAX_CONCURRENT_JOBS + 5,
                          status="pending_review")
    user = {"id": 999, "tenant_id": "cj-pr"}
    _enforce_concurrent_jobs_cap(db, user)  # should not raise


def test_done_jobs_dont_count_toward_cap(db):
    """Completed jobs are terminal — never count."""
    _seed_processing_jobs(db, tenant_id="cj-done",
                          count=100, status="done")
    user = {"id": 999, "tenant_id": "cj-done"}
    _enforce_concurrent_jobs_cap(db, user)  # should not raise


def test_error_jobs_dont_count_toward_cap(db):
    _seed_processing_jobs(db, tenant_id="cj-err",
                          count=20, status="error")
    user = {"id": 999, "tenant_id": "cj-err"}
    _enforce_concurrent_jobs_cap(db, user)  # should not raise


def test_validation_failed_jobs_dont_count_toward_cap(db):
    _seed_processing_jobs(db, tenant_id="cj-vf",
                          count=20, status="validation_failed")
    user = {"id": 999, "tenant_id": "cj-vf"}
    _enforce_concurrent_jobs_cap(db, user)  # should not raise


def test_user_specific_override_higher(db):
    """High-volume tenants (e.g. UMG with full-album batches) can have the cap raised."""
    from database import User

    user = User(
        username="album_batcher",
        hashed_password="x",
        tenant_id="cj-album",
        max_concurrent_jobs=20,
    )
    db.add(user)
    db.flush()

    # 15 in flight — would hit default cap of 10, but user has 20
    _seed_processing_jobs(db, tenant_id="cj-album", count=15)
    _enforce_concurrent_jobs_cap(db, {"id": user.id, "tenant_id": "cj-album"})  # should not raise


def test_user_specific_override_lower(db):
    """Free-tier or risky tenants can have the cap lowered."""
    from database import User

    user = User(
        username="cautious",
        hashed_password="x",
        tenant_id="cj-cautious",
        max_concurrent_jobs=3,
    )
    db.add(user)
    db.flush()

    _seed_processing_jobs(db, tenant_id="cj-cautious", count=3)
    with pytest.raises(HTTPException) as exc:
        _enforce_concurrent_jobs_cap(db, {"id": user.id, "tenant_id": "cj-cautious"})
    assert exc.value.status_code == 429
    assert "3" in exc.value.detail


def test_admin_endpoint_sets_max_concurrent_jobs(client, admin_token):
    """PATCH /admin/users/{id} accepts max_concurrent_jobs."""
    from tests.conftest import auth

    create = client.post("/admin/users", headers=auth(admin_token), json={
        "username": "cj_admin_test",
        "password": "pwd123456789",
        "plan": "100",
    })
    assert create.status_code == 200
    user_id = create.json()["id"]

    res = client.patch(f"/admin/users/{user_id}", headers=auth(admin_token), json={
        "max_concurrent_jobs": 25,
    })
    assert res.status_code == 200
    assert res.json()["max_concurrent_jobs"] == 25


def test_admin_endpoint_clamps_concurrent_jobs_to_min_1(client, admin_token):
    """0 or negative values clamp to 1 (use is_active=False to fully block uploads)."""
    from tests.conftest import auth

    create = client.post("/admin/users", headers=auth(admin_token), json={
        "username": "cj_clamp_test",
        "password": "pwd123456789",
        "plan": "100",
    })
    user_id = create.json()["id"]

    res = client.patch(f"/admin/users/{user_id}", headers=auth(admin_token), json={
        "max_concurrent_jobs": 0,
    })
    assert res.status_code == 200
    assert res.json()["max_concurrent_jobs"] == 1
