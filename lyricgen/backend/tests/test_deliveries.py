"""Tests for the UMG deliveries portal endpoints.

Scope:
  POST /admin/deliveries/from-job/{job_id}   — gating + replace-not-duplicate
  DELETE /admin/deliveries/{id}              — admin JWT path
  GET /api/deliveries/items                  — listing shape + portal token gate
  DELETE /api/deliveries/{id}                — portal token path

Why these tests matter:
  These endpoints are the only way to publish/delete from the UMG portal
  in v2 (the static items.json + gen_page.py flow is gone). A regression
  here means an admin can't ship corrected videos to Universal Music
  through the UI — the exact pain point the feature was meant to fix.

Test approach:
  We stub `storage.object_exists` to True so the POST handler doesn't
  reach R2. R2 is integration-tested elsewhere; we're isolating the
  delivery business logic here.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from tests.conftest import auth


PORTAL_TOKEN = os.environ.get("DELIVERY_PORTAL_TOKEN", "test-portal-token")


@pytest.fixture(autouse=True)
def _portal_token_env():
    """Make sure DELIVERY_PORTAL_TOKEN is set during this module's tests
    so the public endpoints accept our test token. Restored after."""
    old = os.environ.get("DELIVERY_PORTAL_TOKEN")
    os.environ["DELIVERY_PORTAL_TOKEN"] = PORTAL_TOKEN
    yield
    if old is None:
        os.environ.pop("DELIVERY_PORTAL_TOKEN", None)
    else:
        os.environ["DELIVERY_PORTAL_TOKEN"] = old


@pytest.fixture
def approved_job(db, admin_token, client):
    """Create an approved job in the DB that the delivery endpoints can use.

    Bypasses /upload + worker — we just need a row with the right shape.
    """
    from database import Job, User
    # Admin user id from the token
    me = client.get("/auth/me", headers=auth(admin_token)).json()
    job = Job(
        job_id="testjob12345",
        user_id=me["id"],
        tenant_id="default",
        artist="Test Artist",
        song_title="Test Song",
        filename="test.mp3",
        status="done",
        delivery_profile="umg",
        umg_spec={"frame_size": "HD", "fps": 29.97},
        approved_by=me["id"],
        approved_at=datetime.now(timezone.utc),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    yield job
    # Cleanup: remove the job + any deliveries we created against it
    from database import Delivery
    db.query(Delivery).filter(Delivery.job_id == "testjob12345").delete()
    db.query(Job).filter(Job.id == job.id).delete()
    db.commit()


@pytest.fixture
def all_r2_files_present():
    """Stub storage.object_exists → True so the POST handler thinks the
    5 deliverable files are sitting in R2. Real R2 is hit in production."""
    with patch("main.storage.object_exists", return_value=True):
        yield


def test_admin_can_create_delivery(client, admin_token, approved_job, all_r2_files_present):
    res = client.post(
        f"/admin/deliveries/from-job/{approved_job.job_id}",
        headers=auth(admin_token),
        json={},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["job_id"] == approved_job.job_id
    assert body["label"] == "Renderizado"  # first delivery for this song
    assert body["replaced"] is False


def test_non_admin_cannot_create_delivery(client, user_token, approved_job, all_r2_files_present):
    res = client.post(
        f"/admin/deliveries/from-job/{approved_job.job_id}",
        headers=auth(user_token),
        json={},
    )
    assert res.status_code == 403


def test_unapproved_job_rejected(client, admin_token, db, all_r2_files_present):
    from database import Job
    # Same shape as approved_job but status != done.
    me = client.get("/auth/me", headers=auth(admin_token)).json()
    job = Job(
        job_id="pending12345",
        user_id=me["id"],
        tenant_id="default",
        artist="Pending Artist",
        song_title="Pending Song",
        filename="pending.mp3",
        status="pending_review",
        delivery_profile="umg",
    )
    db.add(job)
    db.commit()
    try:
        res = client.post(
            f"/admin/deliveries/from-job/pending12345",
            headers=auth(admin_token),
            json={},
        )
        assert res.status_code == 400
        assert "approved" in res.json()["detail"].lower()
    finally:
        db.query(Job).filter(Job.id == job.id).delete()
        db.commit()


def test_resending_replaces_not_duplicates(client, admin_token, approved_job, all_r2_files_present):
    """Second POST for the same job_id should UPDATE the existing row, not
    create a new one. This was the manual replace-corrected-version workflow
    we used to do by hand in items.json."""
    res1 = client.post(
        f"/admin/deliveries/from-job/{approved_job.job_id}",
        headers=auth(admin_token), json={},
    )
    assert res1.status_code == 200
    first_id = res1.json()["delivery_id"]
    assert res1.json()["replaced"] is False

    res2 = client.post(
        f"/admin/deliveries/from-job/{approved_job.job_id}",
        headers=auth(admin_token), json={"label": "Renderizado v2"},
    )
    assert res2.status_code == 200
    assert res2.json()["replaced"] is True
    assert res2.json()["delivery_id"] == first_id
    assert res2.json()["label"] == "Renderizado v2"


def test_portal_token_required_for_items(client):
    res = client.get("/api/deliveries/items")  # no token
    assert res.status_code == 401


def test_portal_items_lists_active_deliveries(client, admin_token, approved_job, all_r2_files_present):
    # Publish first
    client.post(
        f"/admin/deliveries/from-job/{approved_job.job_id}",
        headers=auth(admin_token), json={},
    )
    # Then list
    res = client.get("/api/deliveries/items", headers={"X-Portal-Token": PORTAL_TOKEN})
    assert res.status_code == 200
    payload = res.json()
    assert "songs" in payload
    assert "file_type_labels" in payload
    # Find our test song in the listing
    songs = [s for s in payload["songs"] if s["artist"] == "Test Artist"]
    assert len(songs) == 1
    versions = songs[0]["versions"]
    assert len(versions) == 1
    v = versions[0]
    assert v["job_id"] == approved_job.job_id
    assert v["label"] == "Renderizado"
    # delivery_id is what the portal uses for DELETE
    assert isinstance(v.get("delivery_id"), int)
    # 5 files expected (umg_master, umg_short, video, short, thumbnail)
    assert len(v["files"]) == 5


def test_portal_can_delete(client, admin_token, approved_job, all_r2_files_present):
    # Publish first
    res = client.post(
        f"/admin/deliveries/from-job/{approved_job.job_id}",
        headers=auth(admin_token), json={},
    )
    delivery_id = res.json()["delivery_id"]

    # Delete via portal endpoint
    res = client.delete(
        f"/api/deliveries/{delivery_id}",
        headers={"X-Portal-Token": PORTAL_TOKEN},
    )
    assert res.status_code == 200

    # Items endpoint should no longer return this delivery
    items = client.get("/api/deliveries/items", headers={"X-Portal-Token": PORTAL_TOKEN}).json()
    songs = [s for s in items["songs"] if s["artist"] == "Test Artist"]
    assert songs == []


def test_admin_delete_via_jwt(client, admin_token, approved_job, all_r2_files_present):
    res = client.post(
        f"/admin/deliveries/from-job/{approved_job.job_id}",
        headers=auth(admin_token), json={},
    )
    delivery_id = res.json()["delivery_id"]

    res = client.delete(f"/admin/deliveries/{delivery_id}", headers=auth(admin_token))
    assert res.status_code == 200


def test_status_endpoint_includes_is_in_umg_portal(
    client, admin_token, approved_job, all_r2_files_present,
):
    """The frontend reads this flag to decide if the "Enviar a UMG" button
    should render as "✓ Ya en UMG" or as the active call-to-action."""
    # Before publish: false
    before = client.get(f"/status/{approved_job.job_id}", headers=auth(admin_token)).json()
    assert before.get("is_in_umg_portal") is False

    # After publish: true
    client.post(
        f"/admin/deliveries/from-job/{approved_job.job_id}",
        headers=auth(admin_token), json={},
    )
    after = client.get(f"/status/{approved_job.job_id}", headers=auth(admin_token)).json()
    assert after.get("is_in_umg_portal") is True
