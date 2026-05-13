"""Backfill ai_provenance with synthetic whisper-1 rows for historical jobs.

Why: until 2026-05-13, transcribe() and _transcribe_via_openai_api() did
NOT wrap the OpenAI call with record_ai_call(), so ai_provenance has zero
"whisper-1" rows. The cost dashboard therefore shows $0 for Whisper —
incorrect since every transcribed job billed OpenAI ~$0.021.

This script:
  1. Lists every job whose lifecycle implies a successful Whisper call
     (status reached one of done / pending_review / rejected / editing,
     i.e. the pipeline got past the transcription step) AND has zero
     existing ai_provenance rows for step="whisper_transcribe".
  2. Inserts ONE synthetic provenance row per such job with:
        step="whisper_transcribe"
        tool_name="whisper-1"
        tool_provider="openai"
        duration_ms=0 (synthetic — no real timing)
        prompt_sent="[backfill 2026-05-13] synthetic row to reflect a
            Whisper-1 transcription that completed before provenance
            tracking was added"
        created_at=Job.created_at (so it lands in the right time window
            for the dashboard's `since_days` filter)

Idempotent: re-runs only target jobs without an existing whisper row.

Run once after deploying the wrapping fix; subsequent jobs get real
rows via record_ai_call() and the script becomes a no-op.
"""
import os, json
from sqlalchemy import create_engine, text

e = create_engine(os.environ["DATABASE_URL"])

# Statuses that imply transcription completed at least once.
TRANSCRIBED_STATUSES = ("done", "pending_review", "rejected", "editing")


with e.connect() as c:
    targets = c.execute(text("""
      SELECT j.job_id, j.created_at
      FROM jobs j
      WHERE j.status = ANY(:statuses)
        AND NOT EXISTS (
          SELECT 1 FROM ai_provenance p
          WHERE p.job_id = j.job_id
            AND p.tool_name = 'whisper-1'
            AND p.tool_provider = 'openai'
        )
      ORDER BY j.created_at ASC
    """), {"statuses": list(TRANSCRIBED_STATUSES)}).fetchall()

print(f"Backfilling whisper-1 provenance for {len(targets)} job(s)...")

PROMPT_NOTE = (
    "[backfill 2026-05-13] synthetic row to reflect a Whisper-1 "
    "transcription that completed before provenance tracking was added"
)

inserted = 0
for job_id, created_at in targets:
    with e.begin() as conn:
        conn.execute(text("""
          INSERT INTO ai_provenance
            (job_id, step, tool_name, tool_provider, prompt_sent,
             duration_ms, created_at)
          VALUES
            (:jid, 'whisper_transcribe', 'whisper-1', 'openai', :prompt,
             0, :created_at)
        """), {
            "jid": job_id,
            "prompt": PROMPT_NOTE,
            "created_at": created_at,
        })
    inserted += 1

print(f"=== Done. Inserted {inserted} whisper-1 row(s). ===")
print()
print("Now the cost dashboard should show ~$0.021 × N whisper calls")
print("under the 'whisper' provider bucket.")
