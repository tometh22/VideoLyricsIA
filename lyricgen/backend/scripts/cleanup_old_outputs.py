"""Sweep `outputs/` to keep local disk bounded.

Why this exists: each Railway replica has an isolated filesystem. Upload
retries therefore happen synchronously inside the worker that rendered the
files; this sweep is deliberately delete-only and must never pretend it can
recover another container's output.

Behaviour per `outputs/<job_id>/` directory:
  - Job done + every deliverable already on R2 + age > KEEP_DONE_MIN
    → delete the dir.
  - Job done + some deliverables missing on R2 + age > KEEP_FAILED_MIN
    → delete locally after the bounded recovery window (the row remains).
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
  CLEANUP_KEEP_FAILED_MIN     default 1440
  CLEANUP_KEEP_STORAGE_FAILED_MIN default 120
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
# transcription_failed incluido (2026-07-02): antes caía al fondo del
# loop sin rama y el WAV de entrada (hasta 150 MB) quedaba en disco para
# SIEMPRE — la row existe, así que nunca es "orphan", y ningún reaper lo
# toca (reap_stuck_transcription conserva el audio a propósito para el
# retry inmediato). _KEEP_FAILED_MIN (24 h default) da ventana de sobra
# para ese retry; el input sigue en R2 para recuperación posterior.
_TERMINAL_FAILED = ("error", "validation_failed", "rejected", "transcription_failed")
_NON_TERMINAL = ("queued", "processing")

_KEEP_DONE_MIN = int(os.environ.get("CLEANUP_KEEP_DONE_MIN", "1440"))
_KEEP_FAILED_MIN = int(os.environ.get("CLEANUP_KEEP_FAILED_MIN", "1440"))
_KEEP_STORAGE_FAILED_MIN = int(os.environ.get("CLEANUP_KEEP_STORAGE_FAILED_MIN", "120"))
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

    scanned = deleted = freed = 0
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
                elif age_min > _KEEP_FAILED_MIN:
                    freed += _delete_dir(
                        job_dir,
                        f"done + R2 incomplete; recovery window expired ({age_min:.0f} min)",
                    )
                    deleted += 1
                continue

            if status in _TERMINAL_FAILED:
                keep_min = (
                    _KEEP_STORAGE_FAILED_MIN
                    if job_dict.get("error_category") == "storage_upload"
                    else _KEEP_FAILED_MIN
                )
                if age_min > keep_min:
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
        "retried": 0,  # backward-compatible summary field; retries are worker-local
        "freed_bytes": freed,
        "freed_mb": round(freed / 1024 / 1024, 1),
        "dry_run": _DRY_RUN,
    }
    logger.info("cleanup_outputs summary: %s", summary)
    return summary


if __name__ == "__main__":
    cleanup()
