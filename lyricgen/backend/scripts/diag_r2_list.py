"""Read-only diagnostic: list R2 objects under a tenant prefix and
cross-check against the Job rows for that tenant.

Detects three failure modes:

  1. DB row points at a key the R2 bucket doesn't have at all
     (file truly deleted / GC'd).
  2. R2 has objects under inputs/<tenant>/<job_id>/ but with a different
     filename than the DB row → sanitization drift, the file is right
     there but the DB key is wrong.
  3. Orphan objects in R2 (job_id has no DB row) — useful to spot leaks.

Usage from the Railway container:

    cd /app
    curl -sL https://raw.githubusercontent.com/tometh22/VideoLyricsIA/main/lyricgen/backend/scripts/diag_r2_list.py | python - --tenant agus.cafisi

Or, if the script is already in the deployed image:

    python scripts/diag_r2_list.py --tenant agus.cafisi
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
    p.add_argument("--tenant", required=True)
    p.add_argument("--limit", type=int, default=200, help="Max R2 keys to list")
    args = p.parse_args()

    import storage  # noqa: E402
    from database import SessionLocal, Job  # noqa: E402

    client = storage._get_client()
    if client is None:
        print("ERROR: R2 not configured (set R2_* env vars).", file=sys.stderr)
        return 2

    prefix = f"inputs/{storage._safe_filename(args.tenant)}/"
    print(f"Listing R2 objects under prefix={prefix!r} (max {args.limit})...")

    paginator = client.get_paginator("list_objects_v2")
    r2_keys: list[tuple[str, int, str]] = []  # (key, size_bytes, last_modified)
    for page in paginator.paginate(Bucket=storage.R2_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []) or []:
            r2_keys.append((obj["Key"], obj["Size"], obj["LastModified"].isoformat()))
            if len(r2_keys) >= args.limit:
                break
        if len(r2_keys) >= args.limit:
            break

    print(f"Found {len(r2_keys)} R2 object(s) under that prefix.\n")
    if r2_keys:
        print(f'{"size_MB":<10} {"last_modified":<30} {"key"}')
        print("-" * 110)
        for key, size, modified in r2_keys[:30]:
            size_mb = round(size / 1024 / 1024, 2)
            print(f"{size_mb:<10} {modified:<30} {key}")
        if len(r2_keys) > 30:
            print(f"... and {len(r2_keys) - 30} more")
        print()

    # Cross-check against DB jobs.
    db = SessionLocal()
    try:
        jobs = (
            db.query(Job)
            .filter(Job.tenant_id == args.tenant)
            .order_by(Job.created_at.desc())
            .limit(args.limit)
            .all()
        )
        db_keys_by_job = {j.job_id: (j.input_r2_key, j.status, j.filename) for j in jobs}

        # R2 keys grouped by the job_id path component.
        def _job_id_of_key(k: str) -> str | None:
            # inputs/<tenant>/<job_id>/<filename>
            parts = k.split("/", 3)
            if len(parts) < 4:
                return None
            return parts[2]

        r2_by_job: dict[str, list[str]] = {}
        for key, _, _ in r2_keys:
            jid = _job_id_of_key(key)
            if jid:
                r2_by_job.setdefault(jid, []).append(key)

        # Failure-mode breakdown
        print("=== Per-job analysis ===")
        print(f'{"job_id":<14} {"db_key_alive":<13} {"r2_files":<9} {"r2_key_matches_db":<18} verdict')
        print("-" * 110)

        for jid, (db_key, status, filename) in db_keys_by_job.items():
            r2_files = r2_by_job.get(jid, [])
            n_files = len(r2_files)
            if not db_key:
                db_state = "NULL"
                matches = "n/a"
            else:
                alive = storage.object_exists(db_key)
                db_state = "alive" if alive else "DEAD"
                matches = "yes" if db_key in r2_files else "no" if n_files else "n/a (empty)"

            if db_state == "alive":
                verdict = "OK"
            elif db_state == "NULL":
                verdict = "no DB link" if n_files == 0 else f"recoverable ({n_files} R2 files)"
            elif db_state == "DEAD" and n_files == 0:
                verdict = "GONE from R2"
            elif db_state == "DEAD" and matches == "no":
                verdict = f"SANITIZATION-DRIFT — R2 has {n_files} file(s) at different key"
            else:
                verdict = f"odd ({n_files} R2 files)"

            print(f"{jid:<14} {db_state:<13} {n_files:<9} {matches:<18} {verdict}")

        # Orphan keys (R2 has but DB doesn't track this job_id at all)
        orphans = [jid for jid in r2_by_job if jid not in db_keys_by_job]
        if orphans:
            print()
            print(f"=== {len(orphans)} orphan job_id prefix(es) in R2 (no DB row) ===")
            for jid in orphans[:10]:
                for key in r2_by_job[jid][:3]:
                    print(f"  {key}")
            if len(orphans) > 10:
                print(f"  ... and {len(orphans) - 10} more orphan job_id prefixes")

        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
