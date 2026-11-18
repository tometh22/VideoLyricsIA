"""Protective, read-only retention inventory for published portal artifacts.

The former cleanup deleted mutable outputs before committing row changes and
could race publication or a reaper in another environment. Until shared-domain
ownership, references and deletion policy are coordinated, this release never
hides deliveries or deletes objects (including when dry_run=False is passed).
The old production reaper is NOT fixed by deploying this module to staging.
"""
from __future__ import annotations

import logging
import os
from collections import Counter
from datetime import datetime, timedelta, timezone

import storage
from database import Delivery, DeliveriesSessionLocal

logger = logging.getLogger("genly.delivery_retention")
DEFAULT_RETENTION_DAYS = 60
RETENTION_DAYS = max(int(os.environ.get("DELIVERY_RETENTION_DAYS", str(DEFAULT_RETENTION_DAYS))), 1)
DELIVERY_FILENAMES = {
    "umg_master": "umg_master.mov", "umg_short": "umg_short.mov",
    "video": "lyric_video.mp4", "short": "short.mp4", "thumbnail": "thumbnail.jpg",
}


def _delivery_keys(delivery: Delivery) -> list[str]:
    """Inventory published references; partial snapshots never fall back.

    Include even extra snapshot entries conservatively: this is a reference
    inventory, not a list of objects authorized for deletion.
    """
    keys = getattr(delivery, "published_file_keys", None)
    if keys is not None:
        return list(dict.fromkeys(key for key in keys.values()
                                  if isinstance(key, str) and key.strip())) if isinstance(keys, dict) else []
    tenant = storage._safe_filename(delivery.tenant_snapshot)
    job_id = storage._safe_filename(delivery.job_id)
    return [f"{tenant}/{job_id}/{storage._safe_filename(DELIVERY_FILENAMES[ft])}"
            for ft in delivery.file_types or [] if ft in DELIVERY_FILENAMES]


def _aware(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def _is_expired(delivery: Delivery, cutoff: datetime) -> bool:
    """Report the existing age rule only; do not change retention policy.

    This legacy rule ignores content_updated_at. Therefore an old row recently
    republished is only a candidate for policy review, never a deletion order.
    """
    anchor = _aware(delivery.removed_at or delivery.added_at)
    return bool(anchor and anchor < _aware(cutoff))


def cleanup_expired_deliveries(*, now: datetime | None = None, dry_run: bool = True) -> dict:
    """Read-only inventory, backwards-compatible entry point for the reaper.

    would_expire: rows matched by the old age rule (not rows to hide).
    would_delete: unique references the old rule would consider (NOT safe).
    protected: all distinct known artifact references, including old snapshots.
    unknown: rows with incomplete/invalid identity or publication metadata.
    No storage calls, DB commit, row mutation, deletion or new deletion policy.
    """
    import database

    shared = bool(database.DELIVERIES_DATABASE_URL)
    report = {
        "status": "shared_database_protected" if shared else "dry_run",
        "dry_run": True, "execution_requested": not dry_run,
        "scanned": 0, "expired": 0, "would_expire": 0, "would_delete": 0,
        "hidden": 0, "deleted": 0, "failed": 0, "protected": 0,
        "protected_deliveries": 0, "unknown": 0, "shared_references": 0,
        "snapshot_deliveries": 0, "retention_days": RETENTION_DAYS,
        "policy": "inventory_only_pending_shared_domain_retention_review",
        "complete": False,
    }
    if not dry_run and not shared:
        report["status"] = "destructive_cleanup_blocked"
    now = _aware(now or datetime.now(timezone.utc))
    cutoff = now - timedelta(days=RETENTION_DAYS)
    db = DeliveriesSessionLocal()
    try:
        deliveries = db.query(Delivery).all()
        references = Counter()
        old_candidates = set()
        for row in deliveries:
            report["scanned"] += 1
            report["protected_deliveries"] += 1
            snapshot = getattr(row, "published_file_keys", None)
            types = row.file_types or []
            unknown = not types or not (row.tenant_snapshot and row.job_id)
            if snapshot is not None:
                report["snapshot_deliveries"] += 1
                unknown = unknown or not isinstance(snapshot, dict)
                if isinstance(snapshot, dict):
                    unknown = unknown or any(not isinstance(snapshot.get(ft), str)
                                             or not snapshot[ft].strip() for ft in types)
            else:
                unknown = unknown or any(ft not in DELIVERY_FILENAMES for ft in types)
            if not (row.removed_at or row.added_at):
                unknown = True
            keys = _delivery_keys(row)
            references.update(set(keys))
            if _is_expired(row, cutoff):
                report["would_expire"] += 1
                old_candidates.update(keys)
            if unknown:
                report["unknown"] += 1
        report["expired"] = report["would_expire"]
        report["would_delete"] = len(old_candidates)
        report["protected"] = len(references)
        report["shared_references"] = sum(count > 1 for count in references.values())
        report["complete"] = True
    except Exception as exc:
        report["status"] = "inventory_failed"
        report["failed"] = 1
        report["error"] = type(exc).__name__
    finally:
        try:
            db.rollback()
        except Exception as exc:
            report.update(status="inventory_failed", complete=False, failed=1,
                          error=type(exc).__name__)
        finally:
            # A failed rollback (e.g. disconnected DB) must still return the
            # connection/session resources; cleanup itself performs no writes.
            try:
                db.close()
            except Exception as exc:
                report.update(status="inventory_failed", complete=False, failed=1,
                              error=type(exc).__name__)
    logger.info("[DELIVERY-RETENTION] read-only inventory status=%s scanned=%s "
                "would_expire=%s protected=%s unknown=%s; no changes executed",
                report["status"], report["scanned"], report["would_expire"],
                report["protected"], report["unknown"])
    return report
