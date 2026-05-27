"""Recover broken Job.input_r2_key bindings by scanning R2 for the
audio still sitting at the expected path.

CONTEXT
-------
The duplicate-job bug (parseFilename frontend/backend mismatch, fixed
in PR #414) created TWO jobs per upload, each with its OWN R2 audio
key under `inputs/<tenant>/<job_id>/<filename>`. When the operator
deleted the "duplicate" via the UI, that R2 file was deleted too —
but the SURVIVING job's R2 file remained intact. In some flows
(notably the cascade where the surviving job's row was the one
deleted, or when a sweep nulled input_r2_key without GC'ing the file)
the DB lost the link while the file stayed in R2 unreachable.

This script reconciles: for every Job row where input_r2_key is NULL
or points to a 404'd R2 key, it checks whether the **expected**
canonical path (built from tenant_id + job_id + filename) has a file
in R2. If yes, it re-binds the DB row.

It's safe in --dry-run (default) — only --apply mutates rows.

USAGE
-----
From Railway shell (recommended — has prod env + R2 creds):

    # Preview what would change for one user
    railway run python scripts/recover_input_keys.py --tenant agus.cafisi

    # Preview across all tenants
    railway run python scripts/recover_input_keys.py

    # Apply for a specific user
    railway run python scripts/recover_input_keys.py --tenant agus.cafisi --apply

    # Apply across all tenants (be careful)
    railway run python scripts/recover_input_keys.py --apply

Locally (against a connected DB / R2):

    cd lyricgen/backend
    python scripts/recover_input_keys.py --tenant agus.cafisi --apply

LIMITATIONS
-----------
- Files purged by R2 lifecycle (`cleanup_old_inputs` at 30 d retention)
  are gone forever — this script cannot resurrect them. For those
  jobs, the MP4 fallback (PR #415) lets the operator keep editing
  using the rendered video's audio track.
- It only restores `input_r2_key`. Other fields (segments_json,
  audio duration cached, waveform cache, etc.) are left untouched —
  they should still be valid from before the link broke.
"""

import argparse
import os
import sys

# Make the script runnable from anywhere via `python scripts/recover_input_keys.py`.
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

import storage  # noqa: E402
from database import SessionLocal, Job  # noqa: E402


def _expected_input_key(job: Job) -> str | None:
    """Compute the canonical R2 path for this job's audio.

    Mirrors `storage._input_object_key` so the script always probes the
    same path the upload endpoint would have used. Returns None when the
    job is missing data we need to build the path.
    """
    if not job.tenant_id or not job.job_id or not job.filename:
        return None
    return storage._input_object_key(job.tenant_id, job.job_id, job.filename)


def _maybe_relink(job: Job, apply: bool) -> str:
    """Inspect a single job and (optionally) re-link its input_r2_key.

    Returns a one-line status string for logging:
      - "skip:no-filename"         — DB row lacks the data we need
      - "skip:current-key-alive"   — already pointing to a valid object
      - "skip:no-r2-file"          — expected path has no file (unrecoverable)
      - "would-relink:<expected>"  — dry-run match
      - "relinked:<expected>"      — committed in --apply mode
    """
    expected = _expected_input_key(job)
    if expected is None:
        return "skip:no-filename"

    # Case A: input_r2_key is set. Check if it's still alive.
    if job.input_r2_key:
        if storage.object_exists(job.input_r2_key):
            return "skip:current-key-alive"
        # The current key is dead. Try the canonical path as a recovery.
        if job.input_r2_key == expected:
            # Already pointed at the canonical path and it's gone. Nothing
            # to recover from R2 (lifecycle/manual delete).
            return "skip:no-r2-file"

    # Case B: input_r2_key is NULL, OR set-but-dead-and-different-from-canonical.
    # Probe the canonical path.
    if not storage.object_exists(expected):
        return "skip:no-r2-file"

    if apply:
        job.input_r2_key = expected
        return f"relinked:{expected}"
    return f"would-relink:{expected}"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tenant", default=None, help="Filter to a specific tenant_id (e.g. 'agus.cafisi').")
    p.add_argument("--user", type=int, default=None, help="Filter to a specific user_id.")
    p.add_argument("--limit", type=int, default=None, help="Stop after inspecting this many jobs.")
    p.add_argument("--apply", action="store_true", help="Actually mutate rows. Default is dry-run.")
    p.add_argument("--verbose", action="store_true", help="Print every job's status, not just recoveries.")
    args = p.parse_args()

    if not storage.is_enabled():
        print("ERROR: R2 not configured (set R2_* env vars).", file=sys.stderr)
        return 2

    db = SessionLocal()
    try:
        # Hunt jobs that LOOK broken: input_r2_key NULL.
        # We DO NOT pre-filter on dead keys (would require R2 probes per
        # job upfront — let _maybe_relink probe lazily).
        q = db.query(Job).filter(Job.input_r2_key.is_(None))
        if args.tenant:
            q = q.filter(Job.tenant_id == args.tenant)
        if args.user is not None:
            q = q.filter(Job.user_id == args.user)
        q = q.order_by(Job.created_at.desc())
        if args.limit:
            q = q.limit(args.limit)

        rows = q.all()
        print(f"Inspecting {len(rows)} job(s) with input_r2_key=NULL"
              f"{f' for tenant={args.tenant}' if args.tenant else ''}"
              f"{f' for user_id={args.user}' if args.user is not None else ''}.")
        print(f"Mode: {'APPLY (will mutate)' if args.apply else 'dry-run (no changes)'}\n")

        counts = {
            "skip:no-filename": 0,
            "skip:current-key-alive": 0,
            "skip:no-r2-file": 0,
            "would-relink": 0,
            "relinked": 0,
        }

        for job in rows:
            status = _maybe_relink(job, apply=args.apply)
            if status.startswith("relinked:"):
                counts["relinked"] += 1
                print(f"  ✅ relinked  job={job.job_id} tenant={job.tenant_id} "
                      f"file={job.filename!r}")
            elif status.startswith("would-relink:"):
                counts["would-relink"] += 1
                print(f"  🔄 would-relink  job={job.job_id} tenant={job.tenant_id} "
                      f"file={job.filename!r}")
            elif args.verbose:
                print(f"  ·  {status:<28s} job={job.job_id}")
                counts[status] = counts.get(status, 0) + 1
            else:
                counts[status] = counts.get(status, 0) + 1

        if args.apply:
            db.commit()
            print(f"\nCommitted {counts['relinked']} relink(s) to the DB.")
        else:
            print(f"\nDry-run complete. {counts['would-relink']} job(s) recoverable.")
            print(f"Re-run with --apply to commit the changes.")

        print(f"\nFinal counts:")
        for k, v in sorted(counts.items()):
            if v:
                print(f"  {k}: {v}")

        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
