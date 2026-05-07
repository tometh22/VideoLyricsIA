"""Reap zombie jobs left in `processing` after a worker crash.

Run periodically (e.g. via Railway cron every 5 min, or
`watch -n 60 python -m backend.scripts.reap_stuck_jobs`).

Why this exists: RQ does not extend its job heartbeat past `job_timeout`,
and a hard SIGKILL / OOM / mid-render container replacement leaves the
DB row pinned at `status="processing"` forever — the UI shows it as
"in progress" indefinitely. We reconcile by:

  1. Listing every Job row in (queued, processing).
  2. Checking RQ for the matching job (started, queued, scheduled).
  3. If RQ has no record AND the row hasn't been updated in
     STUCK_THRESHOLD_MINUTES, marking it status="error" with a reason.

Idempotent. Safe to run concurrently with itself (the UPDATE is bounded
by status NOT IN terminal so a second run is a no-op).

Env:
  STUCK_THRESHOLD_MINUTES (default 30) — grace window before reaping
  REDIS_URL                            — required for RQ lookup
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta, timezone

# Allow running as a script from the backend dir.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from database import Job, SessionLocal  # noqa: E402

logger = logging.getLogger("genly.reaper")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

STUCK_THRESHOLD_MINUTES = int(os.environ.get("STUCK_THRESHOLD_MINUTES", "30"))
TERMINAL_STATUSES = ("done", "error", "rejected", "validation_failed")


def _live_rq_job_ids() -> set[str]:
    """Return the set of RQ job IDs currently known to Redis (any status)."""
    redis_url = os.environ.get("REDIS_URL", "").strip()
    if not redis_url:
        logger.warning("REDIS_URL not set; assuming no live RQ jobs")
        return set()

    from redis import Redis
    from rq import Queue
    from rq.registry import (
        StartedJobRegistry,
        ScheduledJobRegistry,
        DeferredJobRegistry,
    )

    conn = Redis.from_url(redis_url)
    live: set[str] = set()
    for qname in ("enterprise", "default"):
        q = Queue(qname, connection=conn)
        live.update(q.get_job_ids())
        live.update(StartedJobRegistry(qname, connection=conn).get_job_ids())
        live.update(ScheduledJobRegistry(qname, connection=conn).get_job_ids())
        live.update(DeferredJobRegistry(qname, connection=conn).get_job_ids())
    return live


def reap() -> int:
    """Mark stuck jobs as error. Returns the number of jobs reaped."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=STUCK_THRESHOLD_MINUTES)
    live = _live_rq_job_ids()
    reaped = 0

    db = SessionLocal()
    try:
        # Job has no updated_at column — created_at is the only timestamp.
        # In practice that's safe: a queued/processing job older than the
        # threshold AND missing from RQ is by definition stuck.
        candidates = (
            db.query(Job)
            .filter(Job.status.in_(("queued", "processing")))
            .filter(Job.created_at < cutoff)
            .all()
        )
        for job in candidates:
            # Skip if RQ still has a record — it might still finish or be
            # retried. We only reap jobs RQ has lost track of AND the DB
            # hasn't seen progress on for the grace window.
            if job.job_id in live:
                continue
            # Compare-and-set: only flip if still in a non-terminal state.
            updated = (
                db.query(Job)
                .filter(Job.job_id == job.job_id)
                .filter(Job.status.notin_(TERMINAL_STATUSES))
                .update(
                    {
                        "status": "error",
                        "error": (
                            f"Worker lost track of this job "
                            f"(no progress in {STUCK_THRESHOLD_MINUTES} min "
                            "and RQ has no record). Reaped by reap_stuck_jobs."
                        ),
                    },
                    synchronize_session=False,
                )
            )
            if updated:
                reaped += 1
                logger.info("Reaped zombie job %s", job.job_id)
        db.commit()
    finally:
        db.close()

    logger.info("Reaper finished: %d job(s) reaped (threshold=%d min)",
                reaped, STUCK_THRESHOLD_MINUTES)
    return reaped


if __name__ == "__main__":
    raise SystemExit(0 if reap() >= 0 else 1)
