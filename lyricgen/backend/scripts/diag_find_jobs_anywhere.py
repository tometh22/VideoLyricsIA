"""Cross-tenant search for jobs matching a list of song-name needles.

Used in the agus.cafisi cleanup follow-up: 9 of his jobs had no
matching WAV in his folder. Hypothesis was that those songs belong
to OTHER tenants (collaborators). This script confirms or refutes
that by searching every tenant in the DB.

Read-only.

USAGE
-----

From inside the Railway container (or any env with DB creds):

    cd /app
    curl -sL https://raw.githubusercontent.com/tometh22/VideoLyricsIA/main/lyricgen/backend/scripts/diag_find_jobs_anywhere.py | python - \\
      --needle "Mujer amante" \\
      --needle "Runaway" \\
      --needle "Mil Horas" \\
      --needle "Noches Sin Sue"

Or as a quick shortcut for the agus.cafisi case (default needles):

    python scripts/diag_find_jobs_anywhere.py
"""

import argparse
import os
import sys

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) if "__file__" in globals() else os.getcwd()
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)
if "." not in sys.path:
    sys.path.insert(0, ".")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--needle",
        action="append",
        default=None,
        help="Filename substring to search (case-insensitive). Repeatable. "
             "Defaults to the agus.cafisi missing-songs set.",
    )
    p.add_argument("--limit", type=int, default=20, help="Max rows per needle.")
    args = p.parse_args()

    needles = args.needle or [
        "Mujer amante",
        "Runaway",
        "Mil Horas",
        "Noches Sin Sue",
    ]

    import storage  # noqa: E402
    from database import SessionLocal, Job  # noqa: E402

    db = SessionLocal()
    try:
        print(f'{"job_id":<14} {"tenant":<25} {"status":<22} {"key":<6} {"filename"}')
        print("-" * 130)

        for needle in needles:
            rows = (
                db.query(Job)
                .filter(Job.filename.ilike(f"%{needle}%"))
                .order_by(Job.created_at.desc())
                .limit(args.limit)
                .all()
            )
            if not rows:
                print(f"(no jobs found for needle={needle!r})")
                continue

            for r in rows:
                if not r.input_r2_key:
                    ka = "NULL"
                elif storage.object_exists(r.input_r2_key):
                    ka = "alive"
                else:
                    ka = "DEAD"
                fn = (r.filename or "")[:50]
                print(f"{r.job_id:<14} {(r.tenant_id or ''):<25} {(r.status or ''):<22} {ka:<6} {fn}")

            print(f"  ↑ {len(rows)} hit(s) for {needle!r}\n")

        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
