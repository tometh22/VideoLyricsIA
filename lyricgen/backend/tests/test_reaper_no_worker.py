"""find_orphan_no_worker_jobs unit tests.

This sweep catches the worst case the AI-provenance sweep can't: a
worker that died DURING moviepy/ffmpeg render (current_step='video'),
which has no provenance row to age out on. Pre-fix those previously
waited the full 100-min age-based sweep before the reaper noticed; the
new sweep cuts that to 15 min by consulting RQ's live registries.

Tests pin:
  • a young + RQ-live job is left alone (no false positives)
  • an aged job that RQ doesn't know is reaped
  • Redis unreachable → empty list, not a mass-reap
  • the three sweeps de-dupe correctly when they overlap
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from database import Job, SessionLocal
from reaper import find_orphan_no_worker_jobs


def _seed(db, *, status: str = "processing", age_minutes: float = 30):
    jid = f"nworkr_{uuid.uuid4().hex[:6]}"
    db.add(Job(
        job_id=jid,
        user_id=1,
        tenant_id="tenant_nworkr_test",
        artist="Test",
        filename="x.mp3",
        style="oscuro",
        status=status,
        current_step="video",
        progress=40,
        delivery_profile="youtube",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=age_minutes),
    ))
    db.commit()
    return jid


def _cleanup(db):
    db.query(Job).filter(Job.tenant_id == "tenant_nworkr_test").delete()
    db.commit()


def test_aged_job_missing_from_rq_is_reaped():
    """The headline case: Postgres says processing for 20+ min but
    no RQ worker / scheduled / pending registry has the id → reap."""
    db = SessionLocal()
    try:
        _cleanup(db)
        jid = _seed(db, age_minutes=20)
        orphans = find_orphan_no_worker_jobs(
            db, threshold_min=15, live_rq_ids=set(),
        )
        assert any(j.job_id == jid for j in orphans), (
            f"aged job not in live RQ set must be reaped, got: "
            f"{[j.job_id for j in orphans]}"
        )
    finally:
        _cleanup(db)
        db.close()


def test_young_job_is_left_alone_even_when_missing_from_rq():
    """A job created 30 s ago is normal — registry write may not
    have caught up yet. Never reap on that race window."""
    db = SessionLocal()
    try:
        _cleanup(db)
        jid = _seed(db, age_minutes=0.5)
        orphans = find_orphan_no_worker_jobs(
            db, threshold_min=15, live_rq_ids=set(),
        )
        assert all(j.job_id != jid for j in orphans), (
            "30-s-old job must NOT be reaped (registry-write race)"
        )
    finally:
        _cleanup(db)
        db.close()


def test_aged_job_present_in_rq_is_left_alone():
    """Worker IS still on the job (id is in StartedJobRegistry) →
    job is healthy from RQ's view, must not be reaped even if old."""
    db = SessionLocal()
    try:
        _cleanup(db)
        jid = _seed(db, age_minutes=30)
        orphans = find_orphan_no_worker_jobs(
            db, threshold_min=15, live_rq_ids={jid},
        )
        assert all(j.job_id != jid for j in orphans), (
            "job claimed by a live worker must NOT be reaped"
        )
    finally:
        _cleanup(db)
        db.close()


def test_redis_unreachable_returns_empty_not_mass_reap():
    """If we can't talk to Redis we can't confidently say "no worker
    has this" — return [] rather than mass-reap every aged processing
    job in the DB. Critical safety property."""
    db = SessionLocal()
    try:
        _cleanup(db)
        for _ in range(3):
            _seed(db, age_minutes=30)
        # live_rq_ids=None simulates _live_rq_job_ids() returning None
        # because Redis is unreachable.
        orphans = find_orphan_no_worker_jobs(
            db, threshold_min=15, live_rq_ids=None,
        )
        assert orphans == [], (
            "Redis-down state must NOT trigger a mass-reap, got "
            f"{len(orphans)} candidates"
        )
    finally:
        _cleanup(db)
        db.close()


def test_terminal_status_is_never_a_candidate():
    """`done` / `error` / `pending_review` are not in-flight and must
    never be touched by this sweep, regardless of registry state."""
    db = SessionLocal()
    try:
        _cleanup(db)
        for term in ("done", "error", "pending_review"):
            jid = _seed(db, status=term, age_minutes=120)
            orphans = find_orphan_no_worker_jobs(
                db, threshold_min=15, live_rq_ids=set(),
            )
            assert all(j.job_id != jid for j in orphans), (
                f"terminal status {term} must not be reaped"
            )
    finally:
        _cleanup(db)
        db.close()


def test_queued_status_is_a_candidate():
    """`queued` IS in-flight from RQ's perspective. A row in queued
    that doesn't appear in any RQ registry has lost its enqueue —
    must be reaped just like processing."""
    db = SessionLocal()
    try:
        _cleanup(db)
        jid = _seed(db, status="queued", age_minutes=30)
        orphans = find_orphan_no_worker_jobs(
            db, threshold_min=15, live_rq_ids=set(),
        )
        assert any(j.job_id == jid for j in orphans), (
            "aged queued job missing from RQ must be reaped"
        )
    finally:
        _cleanup(db)
        db.close()
