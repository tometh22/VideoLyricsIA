"""Independent release authorization for one-to-many display segmentation.

Lexical/timing repair and v6 analysis certificates cannot authorize this action.
Shadow needs no artifact. Enforce requires a signed, expiring release/config
receipt from the validation runner, plus a fresh job and the per-song contract.
"""
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path

from display_segmentation import POLICY_VERSION, mode
from evidence_attestation import verify_artifact

SCHEMA = "display-segmentation-authorization-v1"
REQUIRED_CHECKS = frozenset({
    "exact_text", "parent_bounds", "protected_work", "concurrency",
    "song_holdout", "manual_boundary_review", "render_parity", "recovery",
})


def runtime_authorization() -> dict:
    result = {"authorized": False, "status": "not_requested"}
    if mode() != "enforce":
        return result
    try:
        path = os.environ.get("DISPLAY_SEGMENTATION_AUTHORIZATION_PATH", "")
        sha = os.environ.get("DISPLAY_SEGMENTATION_AUTHORIZATION_SHA256", "").lower()
        if not path or len(sha) != 64:
            return {**result, "status": "authorization_missing"}
        raw = Path(path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != sha:
            return {**result, "status": "authorization_hash_mismatch"}
        receipt = json.loads(raw)
        trusted, _ = verify_artifact(receipt, "DISPLAY_SEGMENTATION_PUBLIC_KEYS")
        if not trusted:
            return {**result, "status": "authorization_untrusted"}
        from transcription_quality import runtime_identity
        runtime = runtime_identity()
        if (receipt.get("schema") != SCHEMA or receipt.get("policy_version") != POLICY_VERSION
                or receipt.get("decision") != "GO"
                or receipt.get("pipeline_release") != runtime["pipeline_release"]
                or receipt.get("pipeline_config_fingerprint") != runtime["pipeline_config_fingerprint"]):
            return {**result, "status": "authorization_runtime_mismatch"}
        expires = datetime.fromisoformat(str(receipt.get("expires_at", "")).replace("Z", "+00:00"))
        if expires.tzinfo is None or expires <= datetime.now(timezone.utc):
            return {**result, "status": "authorization_expired"}
        checks = receipt.get("checks") or {}
        stats = receipt.get("boundary_validation") or {}
        reviewed, precision = stats.get("reviewed"), stats.get("precision_lower_95")
        if (not all(checks.get(k) is True for k in REQUIRED_CHECKS)
                or type(reviewed) is not int or reviewed < 300
                or type(precision) not in (float, int) or not math.isfinite(precision)
                or not 0.99 <= precision <= 1
                or type(stats.get("damaging")) is not int or stats["damaging"] != 0
                or type(stats.get("holdout_songs")) is not int or stats["holdout_songs"] < 20):
            return {**result, "status": "authorization_validation_incomplete"}
        return {"authorized": True, "status": "authorized", "sha256": sha}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {**result, "status": "authorization_unavailable"}


def fresh_job(db, job) -> bool:
    """Check again under the job lock at persistence, including revision zero.

    An opened editor may already have a document/lease at revision zero. A
    rerun with an existing machine snapshot is not a new transcription.
    """
    from database import EditorDocument
    return bool(
        job is not None and int(job.segments_revision or 0) == 0
        and not job.segments_json and not job.approved_at
        and db.query(EditorDocument.job_id).filter_by(job_id=job.job_id).first() is None
    )


def capture_source(job_id: str, audio_path: str) -> dict:
    """Capture before ASR, release the DB connection before expensive work."""
    from database import Job, SessionLocal
    from quality_cache import sha256_file
    sha = sha256_file(audio_path)
    with SessionLocal() as db:
        row = db.query(Job).filter_by(job_id=job_id).with_for_update().first()
        if row is None:
            raise ValueError("transcription_source_missing")
        if row.input_audio_sha256 and row.input_audio_sha256 != sha:
            raise ValueError("transcription_source_audio_changed")
        row.input_audio_sha256 = sha
        row.audio_revision = max(1, int(row.audio_revision or 0))
        source = {"job_id": job_id, "audio_sha256": sha, "audio_revision": row.audio_revision,
                  "segments_revision": int(row.segments_revision or 0), "fresh": fresh_job(db, row)}
        db.commit()
    return source
