"""Tests for `get_plan_usage` — the pricing model.

Audit 2026-05-28 (UMG onboarding): the operator can iterate on a video
(regenerate background, change typography, adjust lyrics) without each
iteration consuming a slot from their monthly quota. The contract is
"pay per approved video", not "pay per render".

Pre-fix, `get_plan_usage` filtered by `Job.created_at >= month_start`,
which is approximately right but has two real problems:

  1. A job created on May 31 and approved on June 2 was counted against
     May's quota (when it was created) instead of June's (when it was
     paid for). Operators pushing approvals across the boundary got
     billed for the wrong month.

  2. The "approved" semantics weren't documented anywhere — the filter
     happened to align with status="done" because /approve flips both
     fields, but the relationship was implicit. A future change to
     status transitions could silently break the pricing.

The fix swaps the filter to `Job.approved_at >= month_start`, which is
explicit + correct. These tests pin the contract.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid

from auth import create_user, get_plan_usage
from database import Job, SessionLocal, User

_T = "tenant_quota_test"
_USER_ID = 999_001


def _seed_job(
    db, *, job_id, status, approved_at, created_at=None, user_id=_USER_ID,
):
    """Insert a job row with the exact fields the quota filter looks at."""
    if created_at is None:
        # Default: created a few days before approval (when present),
        # or "now" when no approval yet — mirrors real pipeline timing.
        created_at = approved_at - timedelta(days=2) if approved_at else datetime.now(timezone.utc)
    db.add(Job(
        job_id=job_id,
        user_id=user_id,
        tenant_id=_T,
        artist="A",
        filename="t.mp3",
        style="oscuro",
        status=status,
        delivery_profile="youtube",
        created_at=created_at,
        approved_at=approved_at,
    ))
    db.commit()


def _cleanup(db):
    db.query(Job).filter(Job.tenant_id == _T).delete(synchronize_session=False)
    db.commit()


def _this_month_start():
    now = datetime.now(timezone.utc)
    return datetime(now.year, now.month, 1, tzinfo=timezone.utc)


def _approved_this_month(hours_ago=1):
    """An `approved_at` guaranteed to fall in the CURRENT UTC month and in
    the past.

    `now - hours_ago` reads as "approved a bit ago", but in the first
    `hours_ago` hours of day 1 it slips into the PREVIOUS month — and
    `get_plan_usage` filters `approved_at >= month_start`, so the seed
    stops counting (used=0) and the test fails. This bit CI when it ran at
    00:3x UTC on the 1st: `now - 1h` landed on the 31st of the prior month.
    Clamp to `now` (still this month, still past) when the naive value
    would cross the boundary, so the anchor is month-of-day robust."""
    now = datetime.now(timezone.utc)
    candidate = now - timedelta(hours=hours_ago)
    return candidate if candidate >= _this_month_start() else now


def test_pre_approval_does_not_count():
    """A job currently being processed has status != done and
    approved_at = NULL. It must NOT count against quota — the operator
    hasn't committed to it yet.

    Without this guarantee, the operator paying for "X videos approved"
    would see their counter tick up every time a render finished, even
    on drafts they never accept."""
    db = SessionLocal()
    try:
        _cleanup(db)
        # Various non-approved states. None should count.
        for jid, status in [
            ("draft_q", "queued"),
            ("draft_p", "processing"),
            ("draft_r", "pending_review"),
            ("draft_e", "editing"),
            ("draft_f", "transcribed_pending"),
        ]:
            _seed_job(db, job_id=jid, status=status, approved_at=None)
        usage = get_plan_usage(db, user_id=_USER_ID, tenant_id=_T, plan_id="250")
        assert usage["used"] == 0, (
            f"unapproved jobs leaked into quota count "
            f"(used={usage['used']}, expected 0)"
        )
    finally:
        _cleanup(db)
        db.close()


def test_approved_this_month_counts():
    """The base case: a video approved this month counts +1."""
    db = SessionLocal()
    try:
        _cleanup(db)
        _seed_job(
            db, job_id="approved1", status="done",
            approved_at=_approved_this_month(hours_ago=1),
        )
        _seed_job(
            db, job_id="approved2", status="done",
            approved_at=_approved_this_month(hours_ago=2),
        )
        usage = get_plan_usage(db, user_id=_USER_ID, tenant_id=_T, plan_id="250")
        assert usage["used"] == 2, (
            f"expected 2 approved-this-month, got used={usage['used']}"
        )
        assert usage["remaining"] == 248
        assert usage["limit"] == 250
        assert usage["alert_80"] is False
    finally:
        _cleanup(db)
        db.close()


def test_rejected_does_not_count_even_if_previously_approved():
    """Rejected jobs leave approved_at populated (the operator approved
    once, then changed their mind via /reject). The quota filter must
    require BOTH status='done' AND approved_at — without the status
    check, a previously-approved-then-rejected job would slip in.

    This is the edge case the docstring of get_plan_usage explicitly
    flags: rejected after approve still has approved_at set."""
    db = SessionLocal()
    try:
        _cleanup(db)
        # Simulates: approved 1h ago, then rejected just now.
        _seed_job(
            db, job_id="rejected1", status="rejected",
            approved_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        usage = get_plan_usage(db, user_id=_USER_ID, tenant_id=_T, plan_id="250")
        assert usage["used"] == 0, (
            f"rejected job leaked into quota — the status='done' check "
            f"failed to keep it out (used={usage['used']})"
        )
    finally:
        _cleanup(db)
        db.close()


def test_approval_from_previous_month_does_not_count_this_month():
    """The reason for moving from created_at to approved_at: cross-month
    approval timing. A video approved last month is "paid for" in last
    month's billing cycle, not this one.

    This test pins that behaviour by using a date guaranteed to be in
    the previous month — works regardless of which day of the month
    the test runs."""
    db = SessionLocal()
    try:
        _cleanup(db)
        # 45 days ago is always at least one full calendar month back,
        # regardless of when the test runs.
        forty_five_days_ago = datetime.now(timezone.utc) - timedelta(days=45)
        _seed_job(
            db, job_id="last_month", status="done",
            approved_at=forty_five_days_ago,
        )
        usage = get_plan_usage(db, user_id=_USER_ID, tenant_id=_T, plan_id="250")
        assert usage["used"] == 0, (
            "last-month approval counted toward this month — the "
            "approved_at filter is leaking across the month boundary"
        )
    finally:
        _cleanup(db)
        db.close()


def test_created_last_month_approved_this_month_counts_this_month():
    """The reverse of the prior test: when a job is created late in
    one month but approved early in the next, it counts against THIS
    month's quota (the month when the operator paid for it).

    Pre-fix, this scenario was the bug: the operator approved on June 2
    a video created on May 31, and `created_at < month_start` excluded
    it from June's quota even though they were billed for it in June."""
    db = SessionLocal()
    try:
        _cleanup(db)
        forty_days_ago = datetime.now(timezone.utc) - timedelta(days=40)  # last month
        _seed_job(
            db, job_id="crosser", status="done",
            created_at=forty_days_ago,
            approved_at=_approved_this_month(),  # this month, boundary-safe
        )
        usage = get_plan_usage(db, user_id=_USER_ID, tenant_id=_T, plan_id="250")
        assert usage["used"] == 1, (
            "cross-month approval (created last month, approved this "
            "month) didn't count this month — the approved_at filter "
            "is missing the late approval"
        )
    finally:
        _cleanup(db)
        db.close()


def test_other_tenant_isolation():
    """Multi-tenant correctness: tenant A's approvals do NOT appear in
    tenant B's quota. The filter already had tenant_id, this test just
    pins that the new approved_at filter doesn't accidentally drop the
    tenant scope."""
    db = SessionLocal()
    created_user_ids = []
    other_tenant = f"quota_other_{uuid.uuid4().hex[:8]}"
    try:
        _cleanup(db)
        our_user = create_user(
            db,
            username=f"quota_our_{uuid.uuid4().hex[:8]}",
            password="testpass12345",
            tenant_id=_T,
        )
        other_user = create_user(
            db,
            username=f"quota_other_{uuid.uuid4().hex[:8]}",
            password="testpass12345",
            tenant_id=other_tenant,
        )
        created_user_ids = [our_user.id, other_user.id]
        _seed_job(
            db, job_id="our_approval", status="done",
            approved_at=_approved_this_month(),
            user_id=our_user.id,
        )
        # Same shape but different tenant.
        db.add(Job(
            job_id="other_qa",
            user_id=other_user.id, tenant_id=other_tenant,
            artist="X", filename="o.mp3", style="oscuro",
            status="done", delivery_profile="youtube",
            created_at=datetime.now(timezone.utc) - timedelta(hours=3),
            approved_at=_approved_this_month(),
        ))
        db.commit()
        usage = get_plan_usage(
            db,
            user_id=our_user.id,
            tenant_id=_T,
            plan_id="250",
        )
        assert usage["used"] == 1, (
            f"cross-tenant approval leaked into our quota "
            f"(used={usage['used']}, expected 1)"
        )
    finally:
        db.rollback()
        db.query(Job).filter(
            Job.tenant_id.in_([_T, other_tenant])
        ).delete(synchronize_session=False)
        if created_user_ids:
            db.query(User).filter(
                User.id.in_(created_user_ids)
            ).delete(synchronize_session=False)
        db.commit()
        _cleanup(db)
        db.close()
