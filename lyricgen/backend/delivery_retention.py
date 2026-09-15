"""Retention for files published to the UMG delivery portals.

Portal entries are intentionally soft-deleted first so an operator can
remove a delivery without making the R2 bytes unrecoverable immediately.
This module is the delayed hard-delete path: after the configured retention
period it hides old entries and removes only the rendered delivery objects.
Source audio lives under ``inputs/`` and is never touched here because it is
needed by the editor and by re-render/retry flows.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

import storage
from database import Delivery, DeliveriesSessionLocal


logger = logging.getLogger("genly.delivery_retention")

DEFAULT_RETENTION_DAYS = 60
RETENTION_DAYS = max(
    int(os.environ.get("DELIVERY_RETENTION_DAYS", str(DEFAULT_RETENTION_DAYS))),
    1,
)

# Keep this list deliberately separate from the generic job cleanup. These
# are the only output names the delivery portal publishes today. In
# particular, never delete an entire tenant/job prefix: inputs and editor
# previews must survive portal retention.
DELIVERY_FILENAMES = {
    "umg_master": "umg_master.mov",
    "umg_short": "umg_short.mov",
    "video": "lyric_video.mp4",
    "short": "short.mp4",
    "thumbnail": "thumbnail.jpg",
}


def _delivery_keys(delivery: Delivery) -> list[str]:
    tenant = storage._safe_filename(delivery.tenant_snapshot)
    job_id = storage._safe_filename(delivery.job_id)
    keys = []
    for file_type in delivery.file_types or []:
        filename = DELIVERY_FILENAMES.get(file_type)
        if filename:
            keys.append(f"{tenant}/{job_id}/{storage._safe_filename(filename)}")
    return keys


def _is_expired(delivery: Delivery, cutoff: datetime) -> bool:
    """Return whether a row is ready for hard cleanup.

    Active rows expire from the portal based on publish time. Rows manually
    removed earlier expire based on the removal time, giving the team the
    full retention window for accidental deletes and re-downloads.
    """
    anchor = delivery.removed_at or delivery.added_at
    return bool(anchor and anchor < cutoff)


def cleanup_expired_deliveries(*, now: datetime | None = None) -> dict[str, int | str]:
    """Hide and delete deliveries older than ``DELIVERY_RETENTION_DAYS``.

    The caller (the single-runner reaper) supplies cross-replica locking.
    The function remains idempotent: a failed object delete leaves its row
    eligible for the next pass, and deleting an already absent R2 object is
    harmless.
    """
    if not storage.is_enabled():
        return {
            "status": "r2_disabled",
            "scanned": 0,
            "expired": 0,
            "hidden": 0,
            "deleted": 0,
            "failed": 0,
        }

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=RETENTION_DAYS)
    db = DeliveriesSessionLocal()
    try:
        deliveries = db.query(Delivery).all()
        expired = [d for d in deliveries if _is_expired(d, cutoff)]

        # If old duplicate rows exist for a job but a newer row is still
        # visible, keep the objects: the R2 key is job-scoped and deleting it
        # would break the active portal version.
        protected_job_ids = {
            d.job_id for d in deliveries if not _is_expired(d, cutoff)
        }

        hidden = 0
        deleted = 0
        failed = 0
        for delivery in expired:
            if delivery.job_id in protected_job_ids:
                if delivery.removed_at is None:
                    delivery.removed_at = now
                    hidden += 1
                logger.info(
                    "[DELIVERY-RETENTION] kept R2 files for expired row %s; "
                    "job %s still has a newer portal row",
                    delivery.id,
                    delivery.job_id,
                )
                continue

            delete_failed = False
            for key in _delivery_keys(delivery):
                try:
                    storage.delete_object(key)
                    deleted += 1
                except Exception:
                    # Keep going so one transient R2 error does not prevent
                    # the remaining files from being reclaimed. The row
                    # stays eligible and the next daily pass retries it.
                    failed += 1
                    delete_failed = True
                    logger.exception(
                        "[DELIVERY-RETENTION] failed to delete %s (delivery=%s)",
                        key,
                        delivery.id,
                    )
            # Do not advance the retention anchor when an R2 delete failed:
            # an active row remains visible until all its objects are safely
            # reclaimed, and an already-removed row remains eligible on the
            # next daily pass instead of being postponed another 60 days.
            if not delete_failed and delivery.removed_at is None:
                delivery.removed_at = now
                hidden += 1

        if hidden:
            db.commit()
        else:
            db.rollback()

        result = {
            "status": "ok",
            "scanned": len(deliveries),
            "expired": len(expired),
            "hidden": hidden,
            "deleted": deleted,
            "failed": failed,
            "retention_days": RETENTION_DAYS,
        }
        if expired:
            logger.info("[DELIVERY-RETENTION] sweep: %s", result)
        return result
    finally:
        db.close()
