"""Regression for admin review of campaign jobs owned by a different tenant."""
import math
import uuid

import pytest

from database import EditorDocument, EditorVersion, Job, ProductEvent, SessionLocal, User
from product_telemetry import valid_property
from tests.conftest import auth
from tests.test_editor_documents import _token_for, _users_and_job


def _context():
    return {
        "source": "editor", "session_id": str(uuid.uuid4()),
        "position_ms": 8000, "from_position_ms": 22000,
        "line_id": "17", "segment_id": "stable-17", "line_index": 3, "revision": 4, "line_start_ms": 10000,
        "line_context": "target", "review_marker": "quality_window",
        "unsaved_changes": False,
        "requested_lead_in_ms": 2000, "effective_lead_in_ms": 2000,
    }


@pytest.mark.parametrize("admin,expected", [(True, 4), (False, 0)])
def test_cross_tenant_analytics_matches_editor_access_without_editor_writes(client, admin, expected):
    actor, _, _ = _users_and_job(f"seek_actor_{uuid.uuid4().hex[:8]}")
    _, _, job_id = _users_and_job(f"seek_owner_{uuid.uuid4().hex[:8]}")
    with SessionLocal() as db:
        db.query(User).filter_by(id=actor.id).one().role = "admin" if admin else "user"
        # Receiving analytics must not initialize an editor or touch its job.
        db.query(EditorVersion).filter_by(job_id=job_id).delete()
        db.query(EditorDocument).filter_by(job_id=job_id).delete()
        db.commit()
        job_before = dict(db.query(Job).filter_by(job_id=job_id).one().__dict__)
        job_before.pop("_sa_instance_state")
    context = _context()
    seek = {k: v for k, v in context.items() if k not in {"requested_lead_in_ms", "effective_lead_in_ms"}}
    response = client.post("/analytics/events", headers=auth(_token_for(actor)), json={"events": [
        {"name": "editor_line_context_played", "job_id": job_id, "properties": context},
        {"name": "editor_seek", "job_id": job_id, "properties": seek},
        {"name": "editor_autosave_success", "job_id": job_id, "properties": {"checkpoint": "manual", "duration_ms": 3}},
        {"name": "editor_timing_changed", "job_id": job_id, "properties": {"count": 1, "operation": "edit"}},
        {"name": "editor_seek", "job_id": "missing-job", "properties": {"position_ms": 0}},
    ]})
    assert response.status_code == 200
    assert response.json() == {"accepted": expected, "rejected": 5 - expected}
    with SessionLocal() as db:
        events = db.query(ProductEvent).filter_by(job_id=job_id, user_id=actor.id).all()
        assert len(events) == expected
        if admin:
            event = next(e for e in events if e.name == "editor_line_context_played")
            assert event.tenant_id == actor.tenant_id  # retain actor attribution
            assert all(event.properties[k] == v for k, v in context.items())
        assert db.query(EditorDocument).filter_by(job_id=job_id).count() == 0
        assert db.query(EditorVersion).filter_by(job_id=job_id).count() == 0
        job_after = dict(db.query(Job).filter_by(job_id=job_id).one().__dict__)
        job_after.pop("_sa_instance_state")
        assert job_after == job_before


def test_seek_context_accepts_same_tenant_and_rejects_invalid_payloads(client):
    actor, _, job_id = _users_and_job(f"seek_same_{uuid.uuid4().hex[:8]}")
    context = _context()
    invalid = [{"line_id": "raw lyric text"}, {"review_marker": "invented"},
               {"from_position_ms": -1}, {"requested_lead_in_ms": 10001},
               {"lyrics": {"text": "private"}}, {"unsaved_changes": "false"}]
    response = client.post("/analytics/events", headers=auth(_token_for(actor)), json={"events": [
        {"name": "editor_line_context_played", "job_id": job_id, "properties": context},
        *[{"name": "editor_line_context_played", "job_id": job_id,
           "properties": {**context, **bad}} for bad in invalid],
    ]})
    assert response.status_code == 200
    assert response.json() == {"accepted": 1, "rejected": len(invalid)}


@pytest.mark.parametrize("key", ["from_position_ms", "line_start_ms", "requested_lead_in_ms", "effective_lead_in_ms"])
def test_seek_numbers_cannot_poison_metrics(key):
    assert valid_property(key, 0)
    assert valid_property(key, 2000)
    for value in (True, -1, math.nan, math.inf, "song text"):
        assert not valid_property(key, value)
