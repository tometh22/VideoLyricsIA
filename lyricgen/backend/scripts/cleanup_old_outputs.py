"""Sweep `outputs/` to keep local disk bounded.

Why this exists: `pipeline._upload_deliverables_to_r2` only deletes the
local copy AFTER a successful R2 upload. If R2 returns an error mid-
multipart (network hiccup, auth expired, transient 5xx) the local file
stays — and the existing comment in pipeline.py promises a "later
cleanup pass" that never existed. Over weeks of UMG batch deliveries
on a 240 GB Railway instance, that's how the disk fills.

Behaviour per `outputs/<job_id>/` directory:
  - Job done + every deliverable already on R2 + age > KEEP_DONE_MIN
    → delete the dir.
  - Job done + some deliverables missing on R2 (failed upload) + age
    > RETRY_AFTER_MIN → retry the upload via the existing helper. If
    that succeeds, delete locally.
  - Job in (error, validation_failed, rejected) + age > KEEP_FAILED_MIN
    → delete (we keep the audit row in Postgres; the .mp4/.mov is
    no longer useful).
  - Job in (queued, processing, pending_review) → keep, regardless
    of age.
  - Orphan dir (no matching DB row) + age > KEEP_ORPHAN_MIN → delete.

Run as a cron / Railway scheduled task. Idempotent and safe to run
concurrently with itself (each delete is atomic at the FS level).

Env knobs:
  CLEANUP_KEEP_DONE_MIN       default 1440  (24 h)
  CLEANUP_RETRY_FAILED_MIN    default 60    (1 h)
  CLEANUP_KEEP_FAILED_MIN     default 1440
  CLEANUP_KEEP_ORPHAN_MIN     default 60
  CLEANUP_DRY_RUN             default 0     (set to 1 to log only)
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import time
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

logger = logging.getLogger("genly.cleanup_outputs")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

OUTPUTS_DIR = os.path.join(os.path.dirname(_HERE), "..", "outputs")

# Status sets reused below.
_TERMINAL_DONE = ("done", "pending_review")
_TERMINAL_FAILED = ("error", "validation_failed", "rejected")
_NON_TERMINAL = ("queued", "processing")

_KEEP_DONE_MIN = int(os.environ.get("CLEANUP_KEEP_DONE_MIN", "1440"))
_RETRY_FAILED_MIN = int(os.environ.get("CLEANUP_RETRY_FAILED_MIN", "60"))
_KEEP_FAILED_MIN = int(os.environ.get("CLEANUP_KEEP_FAILED_MIN", "1440"))
_KEEP_ORPHAN_MIN = int(os.environ.get("CLEANUP_KEEP_ORPHAN_MIN", "60"))
_DRY_RUN = os.environ.get("CLEANUP_DRY_RUN", "0").strip() in ("1", "true", "yes")

# The deliverable types we expect to see in s3_keys.
_EXPECTED_S3_KEYS_BY_PROFILE = {
    "youtube": ("video", "short", "thumbnail"),
    "umg":     ("video", "short", "umg_master", "umg_short"),
    "both":    ("video", "short", "thumbnail", "umg_master", "umg_short"),
}


def _job_dir_age_minutes(job_dir: str) -> float:
    try:
        mtime = os.path.getmtime(job_dir)
    except OSError:
        return 0.0
    return (time.time() - mtime) / 60.0


def _should_have_keys(job_dict: dict) -> tuple[str, ...]:
    """Which s3_keys are expected based on delivery_profile + actual files
    that landed locally."""
    profile = (job_dict.get("delivery_profile") or "youtube").lower()
    expected = _EXPECTED_S3_KEYS_BY_PROFILE.get(profile, _EXPECTED_S3_KEYS_BY_PROFILE["youtube"])
    # Only count keys for files that actually exist (or used to). We
    # don't want to flag an upload failure for a thumbnail that was never
    # generated for a UMG-only job.
    files = job_dict.get("files") or {}
    return tuple(
        k for k in expected
        if files.get(f"{k}_url") or k in ("umg_master", "umg_short")
    )


def _all_keys_present(job_dict: dict) -> bool:
    s3_keys = job_dict.get("s3_keys") or {}
    return all(s3_keys.get(k) for k in _should_have_keys(job_dict))


def _delete_dir(path: str, reason: str) -> int:
    """Delete a job dir and return bytes freed (0 on dry-run / error)."""
    try:
        size = sum(
            os.path.getsize(os.path.join(root, f))
            for root, _, files in os.walk(path)
            for f in files
            if os.path.isfile(os.path.join(root, f))
        )
    except OSError:
        size = 0
    logger.info("[%s] %s: %s (%d bytes)",
                "dry-run" if _DRY_RUN else "delete", reason, path, size)
    if _DRY_RUN:
        return 0
    try:
        shutil.rmtree(path)
        return size
    except OSError as e:
        logger.warning("rmtree failed for %s: %s", path, e)
        return 0


def _retry_r2_upload(job_id: str, job_dir: str, job_dict: dict) -> bool:
    """Re-attempt the R2 upload for a job whose s3_keys are incomplete.

    Returns True iff every expected key is now present (so the caller
    can delete locally).
    """
    try:
        from pipeline import _upload_deliverables_to_r2
    except Exception as e:
        logger.warning("cannot import _upload_deliverables_to_r2: %s", e)
        return False
    files = job_dict.get("files") or {}
    if not files:
        return False
    logger.info("retrying R2 upload for %s", job_id)
    if _DRY_RUN:
        return False
    try:
        new_keys = _upload_deliverables_to_r2(job_id, job_dir, files)
    except Exception as e:
        logger.warning("R2 retry failed for %s: %s", job_id, e)
        return False
    if not new_keys:
        return False
    # Persist any newly-uploaded keys so /download can serve them.
    try:
        from jobs import update_job, get_job_model
        from database import SessionLocal
        db = SessionLocal()
        try:
            model = get_job_model(db, job_id)
            if model is None:
                return False
            merged = dict(model.s3_keys or {})
            merged.update(new_keys)
            update_job(job_id, s3_keys=merged)
            # Re-evaluate completeness with the merged set.
            return all(merged.get(k) for k in _should_have_keys(job_dict))
        finally:
            db.close()
    except Exception as e:
        logger.warning("update_job after R2 retry failed for %s: %s", job_id, e)
        return False


def cleanup() -> dict:
    """Walk OUTPUTS_DIR, applying the policy. Returns a summary."""
    if not os.path.isdir(OUTPUTS_DIR):
        logger.info("OUTPUTS_DIR %s does not exist; nothing to clean", OUTPUTS_DIR)
        return {"scanned": 0, "deleted": 0, "retried": 0, "freed_bytes": 0}

    try:
        from jobs import get_job_model
        from database import SessionLocal
    except Exception as e:
        logger.error("cannot import jobs.get_job_model: %s", e)
        return {"error": str(e)}

    scanned = deleted = retried = freed = 0
    db = SessionLocal()

    try:
        for entry in os.listdir(OUTPUTS_DIR):
            job_dir = os.path.join(OUTPUTS_DIR, entry)
            if not os.path.isdir(job_dir):
                continue
            scanned += 1
            age_min = _job_dir_age_minutes(job_dir)

            try:
                model = get_job_model(db, entry)
            except Exception as e:
                logger.warning("DB lookup failed for %s: %s", entry, e)
                continue

            if model is None:
                # Orphan — no DB row.
                if age_min > _KEEP_ORPHAN_MIN:
                    freed += _delete_dir(job_dir, f"orphan (age {age_min:.0f} min)")
                    deleted += 1
                continue

            job_dict = model.to_dict()
            status = job_dict.get("status")

            if status in _NON_TERMINAL:
                continue  # job still running, never touch

            if status in _TERMINAL_DONE:
                if _all_keys_present(job_dict) and age_min > _KEEP_DONE_MIN:
                    freed += _delete_dir(
                        job_dir,
                        f"done + R2 complete (age {age_min:.0f} min)",
                    )
                    deleted += 1
                elif age_min > _RETRY_FAILED_MIN:
                    # Some R2 upload failed earlier — retry once.
                    retried += 1
                    if _retry_r2_upload(entry, job_dir, job_dict):
                        freed += _delete_dir(
                            job_dir, "done + R2 retry succeeded",
                        )
                        deleted += 1
                    else:
                        logger.info(
                            "%s: R2 retry incomplete; will try again next cycle",
                            entry,
                        )
                continue

            if status in _TERMINAL_FAILED:
                if age_min > _KEEP_FAILED_MIN:
                    freed += _delete_dir(
                        job_dir,
                        f"{status} (age {age_min:.0f} min)",
                    )
                    deleted += 1
                continue
    finally:
        db.close()

    summary = {
        "scanned": scanned,
        "deleted": deleted,
        "retried": retried,
        "freed_bytes": freed,
        "freed_mb": round(freed / 1024 / 1024, 1),
        "dry_run": _DRY_RUN,
    }
    logger.info("cleanup_outputs summary: %s", summary)
    return summary


if __name__ == "__main__":
    cleanup()
