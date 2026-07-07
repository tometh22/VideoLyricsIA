"""Scheduler daemon for deferred YouTube publishes.

Two scheduling modes exist:

  1. privacy=public + scheduled_at → NOT handled here. The video uploads
     immediately as private with YouTube's native status.publishAt and
     YouTube itself flips it public at the exact time (survives our infra
     being down at fire time).
  2. privacy=unlisted/private + scheduled_at → YouTube can't schedule to
     those targets, so the PublishJob row waits in status "scheduled" and
     this daemon flips due rows to "queued" + enqueues them.

Same shape as the reaper: an in-process daemon thread (started from
main.py on_startup) with a Postgres advisory lock so only one replica
runs the pass. SQLite (tests) no-ops the lock.
"""

import logging
import os
from datetime import datetime, timezone

from database import SessionLocal, PublishJob

logger = logging.getLogger("genly.ytscheduler")

# Distinct from the reaper's ...101 (see plan: scheduler=...102,
# analytics=...103).
_SCHEDULER_ADVISORY_LOCK_KEY = 9118364455199102

SCHEDULER_INTERVAL_S = int(os.environ.get("YT_SCHEDULER_INTERVAL_S", "60"))


def run_due_publishes() -> int:
    """One pass: enqueue every scheduled publish whose time has come.
    Owns its session. Returns the number of publishes enqueued."""
    import queue_jobs

    db = SessionLocal()
    try:
        if db.bind.dialect.name == "postgresql":
            from sqlalchemy import text
            got = db.execute(
                text("SELECT pg_try_advisory_lock(:k)"),
                {"k": _SCHEDULER_ADVISORY_LOCK_KEY},
            ).scalar()
            if not got:
                logger.debug("yt-scheduler: another replica holds the lock; skipping")
                return 0

        now = datetime.now(timezone.utc)
        due = (
            db.query(PublishJob)
            .filter(
                PublishJob.status == "scheduled",
                PublishJob.scheduled_at <= now,
            )
            .order_by(PublishJob.scheduled_at.asc())
            .limit(50)
            .all()
        )

        enqueued = 0
        for row in due:
            # Conditional flip: a concurrent cancel (or another pass on a
            # non-Postgres setup) may have raced us.
            flipped = (
                db.query(PublishJob)
                .filter(PublishJob.id == row.id, PublishJob.status == "scheduled")
                .update({PublishJob.status: "queued"}, synchronize_session=False)
            )
            db.commit()
            if not flipped:
                continue
            try:
                queue_jobs.enqueue_publish(row.id)
                enqueued += 1
            except Exception as e:
                # Put it back so the next pass retries instead of losing it.
                logger.error("yt-scheduler: enqueue failed for %s: %s", row.id, e)
                db.query(PublishJob).filter(
                    PublishJob.id == row.id, PublishJob.status == "queued",
                ).update({PublishJob.status: "scheduled"}, synchronize_session=False)
                db.commit()
        return enqueued
    finally:
        try:
            if db.bind.dialect.name == "postgresql":
                from sqlalchemy import text
                db.execute(
                    text("SELECT pg_advisory_unlock(:k)"),
                    {"k": _SCHEDULER_ADVISORY_LOCK_KEY},
                )
                db.commit()
        except Exception:  # pragma: no cover
            pass
        db.close()
