#!/usr/bin/env python3
"""Fail worker startup until structural columns required by this release exist."""
import os
import time

from sqlalchemy import text

from database import engine


REQUIRED_COLUMNS = {
    ("jobs", "transcription_quality"),
    ("jobs", "machine_snapshot_required"),
    ("jobs", "quality_learning_epoch"),
    ("jobs", "quality_learning_invalidated_at"),
    ("jobs", "input_audio_sha256"),
    ("jobs", "input_audio_etag"),
    ("jobs", "audio_revision"),
    ("jobs", "active_quality_attempt_id"),
    ("jobs", "active_pipeline_attempt_id"),
    ("jobs", "error_code"),
    ("jobs", "active_transcription_attempt_id"),
    ("jobs", "input_r2_key"),
    ("jobs", "segments_json"),
    ("jobs", "render_params"),
    ("jobs", "last_progress_at"),
    ("editor_documents", "quality_proposal"),
    ("editor_documents", "machine_evidence"),
    ("editor_versions", "provenance"),
    ("job_outbox_events", "dedupe_key"),
    ("job_outbox_events", "available_at"),
    ("job_outbox_events", "processing_at"),
    ("job_outbox_events", "processing_token"),
    ("job_outbox_events", "consumed_at"),
    ("correction_observations", "hmac_key_id"),
    ("quality_patterns", "fingerprint"),
    ("quality_fix_proposals", "candidate_config"),
    ("quality_experiment_runs", "candidate_config_hash"),
}


try:
    timeout_s = min(900.0, max(10.0, float(
        os.environ.get("WORKER_SCHEMA_WAIT_TIMEOUT_SECONDS", "600")
    )))
except ValueError:
    timeout_s = 600.0
deadline = time.monotonic() + timeout_s
delay_s = 1.0

while True:
    try:
        with engine.connect() as connection:
            rows = connection.execute(text("""
                SELECT table_name, column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
            """)).all()
        present = {(row[0], row[1]) for row in rows}
        missing = REQUIRED_COLUMNS - present
        if not missing:
            print("[schema] worker requirements satisfied")
            break
        detail = ", ".join(
            f"{table}.{column}" for table, column in sorted(missing)
        )
    except Exception as exc:
        detail = f"database unavailable: {type(exc).__name__}"

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SystemExit(
            f"worker schema was not ready after {timeout_s:.0f}s: {detail}"
        )
    sleep_s = min(delay_s, remaining, 10.0)
    print(f"[schema] waiting {sleep_s:.1f}s for API migration: {detail}")
    time.sleep(sleep_s)
    delay_s = min(10.0, delay_s * 2)
