"""YouTube Data API daily-quota accounting.

Quota is PER GOOGLE CLOUD PROJECT (not per channel) and resets at
midnight America/Los_Angeles. Default project quota is 10,000 units/day;
videos.insert alone costs 1,600 (~6 uploads/day) — so the publish worker
reserves budget BEFORE uploading and defers the job to the next Pacific
day when the budget would be exceeded, instead of burning an upload
attempt into a quotaExceeded error.

Postgres (not Redis) on purpose: a handful of atomic increments per
upload, shared by API + all workers, surviving Redis restarts.
"""

import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from database import SessionLocal, YouTubeApiQuota

_PACIFIC = ZoneInfo("America/Los_Angeles")

DAILY_LIMIT = int(os.environ.get("YOUTUBE_DAILY_QUOTA_UNITS", "10000"))
ALERT_PCT = int(os.environ.get("YOUTUBE_QUOTA_ALERT_PCT", "80"))

UNIT_COSTS = {
    "videos.insert": 1600,
    "videos.update": 50,
    "thumbnails.set": 50,
    "videos.list": 1,
    "channels.list": 1,
}

# Budget a publish reserves up front: the insert plus headroom for the
# thumbnail set that follows it.
PUBLISH_RESERVATION = UNIT_COSTS["videos.insert"] + UNIT_COSTS["thumbnails.set"]


def _quota_date_today() -> str:
    return datetime.now(_PACIFIC).date().isoformat()


def next_pacific_midnight() -> datetime:
    """UTC datetime of the next quota reset (+5 min of slack)."""
    now_pacific = datetime.now(_PACIFIC)
    tomorrow = (now_pacific + timedelta(days=1)).replace(
        hour=0, minute=5, second=0, microsecond=0,
    )
    return tomorrow.astimezone(timezone.utc)


def _ensure_row(s, quota_date: str) -> YouTubeApiQuota:
    row = s.query(YouTubeApiQuota).filter(YouTubeApiQuota.quota_date == quota_date).first()
    if row is None:
        row = YouTubeApiQuota(quota_date=quota_date, units_used=0)
        s.add(row)
        try:
            s.commit()
        except Exception:
            # Another process created it in the same instant.
            s.rollback()
            row = s.query(YouTubeApiQuota).filter(YouTubeApiQuota.quota_date == quota_date).one()
    return row


def record_usage(op: str, count: int = 1) -> int:
    """Add `op`'s cost to today's counter. Returns the new total.
    Fires the operator alert email once per day when crossing ALERT_PCT."""
    units = UNIT_COSTS.get(op, 1) * count
    quota_date = _quota_date_today()
    s = SessionLocal()
    try:
        _ensure_row(s, quota_date)
        s.query(YouTubeApiQuota).filter(YouTubeApiQuota.quota_date == quota_date).update(
            {YouTubeApiQuota.units_used: YouTubeApiQuota.units_used + units},
            synchronize_session=False,
        )
        s.commit()
        total = (
            s.query(YouTubeApiQuota.units_used)
            .filter(YouTubeApiQuota.quota_date == quota_date)
            .scalar()
        )
        _maybe_alert(s, quota_date, total)
        return total
    finally:
        s.close()


def check_and_reserve(op: str, extra_units: int = 0) -> bool:
    """Atomically reserve `op`'s cost (+extra) against today's budget.
    Returns False when the budget would be exceeded — the caller defers.

    Reserve-before-call: two concurrent uploads can't both squeeze into
    the last slot. Over-counting on an upload that later fails is the
    safe direction (no refunds in v1)."""
    units = UNIT_COSTS.get(op, 1) + extra_units
    quota_date = _quota_date_today()
    s = SessionLocal()
    try:
        _ensure_row(s, quota_date)
        reserved = (
            s.query(YouTubeApiQuota)
            .filter(
                YouTubeApiQuota.quota_date == quota_date,
                YouTubeApiQuota.units_used + units <= DAILY_LIMIT,
            )
            .update(
                {YouTubeApiQuota.units_used: YouTubeApiQuota.units_used + units},
                synchronize_session=False,
            )
        )
        s.commit()
        if reserved:
            total = (
                s.query(YouTubeApiQuota.units_used)
                .filter(YouTubeApiQuota.quota_date == quota_date)
                .scalar()
            )
            _maybe_alert(s, quota_date, total)
        return bool(reserved)
    finally:
        s.close()


def get_usage() -> dict:
    quota_date = _quota_date_today()
    s = SessionLocal()
    try:
        used = (
            s.query(YouTubeApiQuota.units_used)
            .filter(YouTubeApiQuota.quota_date == quota_date)
            .scalar()
        ) or 0
    finally:
        s.close()
    remaining = max(0, DAILY_LIMIT - used)
    return {
        "quota_date": quota_date,
        "units_used": used,
        "limit": DAILY_LIMIT,
        "pct": int(used * 100 / DAILY_LIMIT) if DAILY_LIMIT else 0,
        "uploads_remaining": remaining // PUBLISH_RESERVATION,
    }


def _maybe_alert(s, quota_date: str, total: int) -> None:
    """Email the operator once per Pacific day when crossing ALERT_PCT.
    The conditional flip of alert_sent makes exactly one process send."""
    if total < DAILY_LIMIT * ALERT_PCT / 100:
        return
    flipped = (
        s.query(YouTubeApiQuota)
        .filter(
            YouTubeApiQuota.quota_date == quota_date,
            YouTubeApiQuota.alert_sent.is_(False),
        )
        .update({YouTubeApiQuota.alert_sent: True}, synchronize_session=False)
    )
    s.commit()
    if not flipped:
        return
    try:
        import emails
        owner = os.environ.get("OWNER_EMAIL", "tomas@epical.digital")
        emails.send_youtube_quota_alert(owner, total, DAILY_LIMIT)
    except Exception as e:  # pragma: no cover — alerting must never break uploads
        print(f"[QUOTA] alert email failed: {e}")
