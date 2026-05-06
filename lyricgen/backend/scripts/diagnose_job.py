"""Quick diagnostic for a job that looks stuck in the UI.

Use case: operator says "el job lleva X min y la barra no se mueve". The
worker logs are silent during moviepy compositing (the C extensions
don't yield to Python's logger), so railway logs alone won't tell us
whether the job is making progress or genuinely hung.

This script reads the `jobs` row from Postgres directly:
  - status (queued / processing / pending_review / done / error)
  - progress (integer 0–100, updated by the pipeline at each step)
  - current_step (whisper / lyrics / video / short / thumbnail / validation)
  - age (seconds since created_at)
  - error (if set)

Usage:
    cd lyricgen/backend
    source venv/bin/activate
    export DATABASE_URL=postgresql://...   # or run via railway
    python scripts/diagnose_job.py <job_id>

Or, the same query directly via railway (no python needed):
    echo "SELECT progress, current_step, status, EXTRACT(EPOCH FROM (NOW() - created_at)) AS age \\
          FROM jobs WHERE job_id='<JOB_ID>';" | railway connect Postgres -e production

Decision rules baked into the output:
  - progress changing across two calls 30 s apart → job is alive
  - progress stuck for > 10 min in {video, short} → moviepy hang likely;
    cancel + retry with a different background (jpg paths are faster
    and less prone to the hang than mp4 palindrome paths)
  - progress stuck for > 15 min in {whisper, lyrics, validation} →
    different cause; check worker logs for a Python traceback
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def fetch(db, job_id: str):
    from database import Job
    return db.query(Job).filter(Job.job_id == job_id).first()


def fmt(job) -> str:
    if not job:
        return "<not found>"
    age = ""
    if job.created_at:
        delta = time.time() - job.created_at.timestamp()
        age = f" age={delta:.0f}s ({delta / 60:.1f} min)"
    parts = [
        f"job={job.job_id}",
        f"status={job.status}",
        f"progress={job.progress}",
        f"step={job.current_step or '-'}",
    ]
    out = " ".join(parts) + age
    if job.error:
        out += f"\n  error: {job.error[:200]}"
    if job.video_url:
        out += f"\n  video_url: {job.video_url}"
    return out


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python scripts/diagnose_job.py <job_id> [--watch]")
        return 1
    job_id = sys.argv[1]
    watch = "--watch" in sys.argv[2:]

    if not os.environ.get("DATABASE_URL"):
        print("ERROR: DATABASE_URL not set. Try: railway run python scripts/diagnose_job.py")
        return 1

    from database import SessionLocal
    db = SessionLocal()
    try:
        if not watch:
            print(fmt(fetch(db, job_id)))
            return 0

        # Watch mode: poll every 30 s, print only when status or progress change.
        prev_state = None
        while True:
            db.expire_all()
            job = fetch(db, job_id)
            if not job:
                print("<not found>")
                return 1
            state = (job.status, job.progress, job.current_step)
            if state != prev_state:
                print(time.strftime("%H:%M:%S "), fmt(job))
                prev_state = state
            if job.status in ("done", "error", "pending_review", "rejected", "validation_failed"):
                return 0
            time.sleep(30)
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
