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
RETIRED_MANIFEST_GRACE_DAYS = 8  # portal signed URLs last up to seven days

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


def cleanup_expired_deliveries(
    *, now: datetime | None = None, dry_run: bool = False,
) -> dict[str, int | str | bool]:
    """Hide and delete deliveries older than ``DELIVERY_RETENTION_DAYS``.

    The caller (the single-runner reaper) supplies cross-replica locking.
    The function remains idempotent: a failed object delete leaves its row
    eligible for the next pass, and deleting an already absent R2 object is
    harmless.
    """
    if not storage.is_enabled():
        return {
            "status": "r2_disabled",
            "dry_run": dry_run,
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
        planned_hidden = 0
        planned_delete = 0
        metadata_changed = False
        for delivery in expired:
            if delivery.job_id in protected_job_ids:
                if delivery.removed_at is None:
                    planned_hidden += 1
                    if not dry_run:
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
                planned_delete += 1
                if dry_run:
                    continue
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
                planned_hidden += 1
                if not dry_run:
                    delivery.removed_at = now
                    hidden += 1

        # Published snapshots are shared by Argentina and Chile when both
        # portals expose the same render. Keep current snapshots and retired
        # snapshots while an issued signed URL may still be valid.
        grace_cutoff = now - timedelta(days=RETIRED_MANIFEST_GRACE_DAYS)
        protected_snapshot_keys: set[str] = set()
        for delivery in deliveries:
            if not _is_expired(delivery, cutoff):
                protected_snapshot_keys.update(
                    (delivery.published_file_keys or {}).values()
                )
            for retired in delivery.retired_file_keys or []:
                try:
                    retired_at = datetime.fromisoformat(retired["retired_at"])
                    if retired_at.tzinfo is None:
                        retired_at = retired_at.replace(tzinfo=timezone.utc)
                except (KeyError, TypeError, ValueError):
                    if isinstance(retired, dict):
                        protected_snapshot_keys.update((retired.get("keys") or {}).values())
                    continue
                if retired_at > grace_cutoff:
                    protected_snapshot_keys.update((retired.get("keys") or {}).values())

        for delivery in deliveries:
            keep_retired = []
            for retired in delivery.retired_file_keys or []:
                try:
                    retired_at = datetime.fromisoformat(retired["retired_at"])
                    if retired_at.tzinfo is None:
                        retired_at = retired_at.replace(tzinfo=timezone.utc)
                except (KeyError, TypeError, ValueError):
                    keep_retired.append(retired)
                    continue
                keys = list((retired.get("keys") or {}).values())
                if retired_at > grace_cutoff or any(key in protected_snapshot_keys for key in keys):
                    keep_retired.append(retired)
                    continue
                try:
                    for key in set(keys):
                        if key.startswith("published/"):
                            planned_delete += 1
                            if not dry_run:
                                storage.delete_object(key)
                                deleted += 1
                except Exception:
                    failed += 1
                    keep_retired.append(retired)
                    logger.exception("[DELIVERY-RETENTION] failed to retire delivery=%s", delivery.id)
            if not dry_run and keep_retired != (delivery.retired_file_keys or []):
                delivery.retired_file_keys = keep_retired
                metadata_changed = True

            if _is_expired(delivery, cutoff) and delivery.published_file_keys:
                current_keys = set(delivery.published_file_keys.values())
                if not any(key in protected_snapshot_keys for key in current_keys):
                    try:
                        for key in current_keys:
                            if key.startswith("published/"):
                                planned_delete += 1
                                if not dry_run:
                                    storage.delete_object(key)
                                    deleted += 1
                        if not dry_run:
                            delivery.published_file_keys = None
                            delivery.published_file_etags = None
                            metadata_changed = True
                    except Exception:
                        failed += 1
                        logger.exception("[DELIVERY-RETENTION] failed to remove snapshot delivery=%s", delivery.id)

        if dry_run:
            db.rollback()
        elif hidden or metadata_changed:
            db.commit()
        else:
            db.rollback()

        result = {
            "status": "ok",
            "dry_run": dry_run,
            "scanned": len(deliveries),
            "expired": len(expired),
            "hidden": hidden,
            "deleted": deleted,
            "failed": failed,
            "planned_hidden": planned_hidden,
            "planned_delete": planned_delete,
            "retention_days": RETENTION_DAYS,
        }
        if expired:
            logger.info("[DELIVERY-RETENTION] sweep: %s", result)
        return result
    finally:
        db.close()
