"""Read-only diagnostic: print job state for a tenant.

Shows, per job (latest 50 by default):
  - status
  - whether input_r2_key is set + ALIVE in R2 (HEAD probe)
  - whether s3_keys["video"] exists (potential MP4 fallback target)
  - filename

Run from the Railway container (or any env with DB + R2 creds):

    cd /app
    python scripts/diag_jobs.py --tenant agus.cafisi
    python scripts/diag_jobs.py --tenant agus.cafisi --limit 100
    python scripts/diag_jobs.py --tenant agus.cafisi --only-broken

Or pipe from GitHub raw (no redeploy needed if main is current):

    cd /app
    curl -sL https://raw.githubusercontent.com/tometh22/VideoLyricsIA/main/lyricgen/backend/scripts/diag_jobs.py | python - --tenant agus.cafisi

Does not mutate anything.
"""

import argparse
import os
import sys

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) if "__file__" in globals() else os.getcwd()
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)
# Also include cwd for the curl-pipe-to-stdin invocation, since
# __file__ is "<stdin>" in that case and abspath gets weird.
if "." not in sys.path:
    sys.path.insert(0, ".")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--tenant", required=True)
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--only-broken", action="store_true",
                   help="Only show jobs whose input_r2_key is NULL or DEAD.")
    args = p.parse_args()

    import storage  # noqa: E402
    from database import SessionLocal, Job  # noqa: E402

    db = SessionLocal()
    try:
        rows = (
            db.query(Job)
            .filter(Job.tenant_id == args.tenant)
            .order_by(Job.created_at.desc())
            .limit(args.limit)
            .all()
        )
        print(f"Total jobs for tenant={args.tenant!r} (last {args.limit}): {len(rows)}")
        print()
        header = f'{"job_id":<14} {"status":<22} {"key":<6} {"video":<6} {"created":<11} {"filename"}'
        print(header)
        print("-" * len(header))
        shown = 0
        counts = {"NULL": 0, "alive": 0, "DEAD": 0}
        for j in rows:
            if not j.input_r2_key:
                ka = "NULL"
            elif storage.object_exists(j.input_r2_key):
                ka = "alive"
            else:
                ka = "DEAD"
            counts[ka] += 1
            if args.only_broken and ka == "alive":
                continue
            hv = bool((j.s3_keys or {}).get("video"))
            fn = (j.filename or "")[:55]
            created = j.created_at.strftime("%Y-%m-%d") if j.created_at else ""
            print(f"{j.job_id:<14} {(j.status or ''):<22} {ka:<6} {str(hv):<6} {created:<11} {fn}")
            shown += 1
        print()
        print(f"Summary:  NULL={counts['NULL']}  alive={counts['alive']}  DEAD={counts['DEAD']}")
        if args.only_broken:
            print(f"Shown (broken only): {shown}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
