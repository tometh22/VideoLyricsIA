"""Tests for per-tenant daily volume cap (UMG-readiness, premise #5).

The cap prevents a runaway from creating large Veo bills during a single
day. Default is DEFAULT_DAILY_CAP (50/day); admins can override per-user
via PATCH /admin/users/{id}.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from main import _enforce_daily_volume_cap, DEFAULT_DAILY_CAP


def _seed_jobs(db, *, tenant_id: str, count: int, hours_ago: float = 0):
    """Insert N jobs for a tenant at a specific point in time. Uses flush()
    (not commit()) so the conftest db fixture's rollback cleans up after
    each test, preventing job_id collisions across tests."""
    from database import Job

    when = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    # Job.job_id is a 12-char unique field. Use the tenant_id hash + index so
    # multiple tests with different tenant_ids never collide.
    import hashlib
    prefix = hashlib.sha1(tenant_id.encode()).hexdigest()[:6]
    for i in range(count):
        db.add(Job(
            job_id=f"{prefix}{i:05d}"[:12],
            user_id=1,
            tenant_id=tenant_id,
            artist="Test",
            filename="x.mp3",
            status="done",
            created_at=when,
        ))
    db.flush()


def test_cap_default_is_50():
    """Sanity check on the system default — premise #5 of the design doc."""
    assert DEFAULT_DAILY_CAP == 50


def test_under_cap_does_not_raise(db):
    _seed_jobs(db, tenant_id="cap-under", count=10)
    user = {"id": 999, "tenant_id": "cap-under"}
    # Should not raise
    _enforce_daily_volume_cap(db, user)


def test_at_cap_raises_429(db):
    _seed_jobs(db, tenant_id="cap-exact", count=DEFAULT_DAILY_CAP)
    user = {"id": 999, "tenant_id": "cap-exact"}
    with pytest.raises(HTTPException) as exc:
        _enforce_daily_volume_cap(db, user)
    assert exc.value.status_code == 429
    assert "cap reached" in exc.value.detail.lower()


def test_over_cap_raises_429(db):
    _seed_jobs(db, tenant_id="cap-over", count=DEFAULT_DAILY_CAP + 5)
    user = {"id": 999, "tenant_id": "cap-over"}
    with pytest.raises(HTTPException) as exc:
        _enforce_daily_volume_cap(db, user)
    assert exc.value.status_code == 429


def test_jobs_older_than_24h_dont_count(db):
    _seed_jobs(db, tenant_id="cap-stale", count=DEFAULT_DAILY_CAP, hours_ago=25)
    user = {"id": 999, "tenant_id": "cap-stale"}
    # Should not raise — all jobs are >24h old
    _enforce_daily_volume_cap(db, user)


def test_per_user_override_higher_than_default(db, client, admin_token):
    """Admin can raise the cap for a high-volume tenant. Below is a black-box
    test that PATCH /admin/users/{id} can set max_videos_per_day."""
    from tests.conftest import auth

    # Create a test user
    create = client.post("/admin/users", headers=auth(admin_token), json={
        "username": "highvol",
        "password": "pwd123456789",
        "plan": "100",
    })
    assert create.status_code == 200, create.text
    user_id = create.json()["id"]

    # Set their cap to 500
    res = client.patch(f"/admin/users/{user_id}", headers=auth(admin_token), json={
        "max_videos_per_day": 500,
    })
    assert res.status_code == 200, res.text
    assert res.json()["max_videos_per_day"] == 500


def test_per_user_override_clamps_negative_to_zero(db, client, admin_token):
    """A negative value should be clamped to 0 (effectively block all uploads)."""
    from tests.conftest import auth

    create = client.post("/admin/users", headers=auth(admin_token), json={
        "username": "clamp",
        "password": "pwd123456789",
        "plan": "100",
    })
    user_id = create.json()["id"]

    res = client.patch(f"/admin/users/{user_id}", headers=auth(admin_token), json={
        "max_videos_per_day": -10,
    })
    assert res.status_code == 200
    assert res.json()["max_videos_per_day"] == 0


def test_user_specific_cap_used_when_set(db):
    """If User.max_videos_per_day is set to a small value, the cap kicks in
    earlier than the default."""
    from database import User

    # Create a user with a tight cap
    user = User(
        username="captest",
        hashed_password="x",
        tenant_id="cap-user-specific",
        max_videos_per_day=3,
    )
    db.add(user)
    db.flush()

    _seed_jobs(db, tenant_id="cap-user-specific", count=3)

    user_dict = {"id": user.id, "tenant_id": "cap-user-specific"}
    with pytest.raises(HTTPException) as exc:
        _enforce_daily_volume_cap(db, user_dict)
    assert exc.value.status_code == 429
