"""Off-Railway Postgres backup — pg_dump every few hours, stored in R2.

Runs from .github/workflows/db-backup.yml every 6h. This is a LOWER-RPO,
OFF-RAILWAY layer ON TOP OF Railway's native daily/weekly/monthly volume
backups — not a replacement:

  - Railway native backups live inside Railway. If Railway has a
    catastrophic failure, those are exactly what you can't reach. This
    dump lives in Cloudflare R2 (separate provider).
  - Native backups are daily at finest. This runs every 6h, so worst-case
    data loss drops from ~24h to ~6h.
  - A logical pg_dump (-Fc) is portable: restorable to ANY Postgres
    (including a future GCP Cloud SQL), unlike a Railway volume snapshot.

Why we built this: 2026-05-20 the prod DB had ZERO backups and Railway
had two edge outages in one day with UMG live. Backups are the floor
under everything else.

Retention: keeps the newest _KEEP dumps in R2 (7 days at 6h cadence),
prunes older ones so storage stays bounded. Railway's native
weekly/monthly cover long-term retention.

Restore (when you need it):
    aws s3 cp s3://$R2_BUCKET/db-backups/<file>.dump ./restore.dump \\
        --endpoint-url $R2_ENDPOINT_URL
    pg_restore --no-owner --no-acl --clean --if-exists \\
        -d "$TARGET_DATABASE_URL" restore.dump

Env vars:
  DATABASE_URL          public Postgres connection string (pg_dump source)
  R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY / R2_ENDPOINT_URL / R2_BUCKET
  BACKUP_PREFIX         R2 key prefix (default "db-backups")
  BACKUP_KEEP           how many dumps to retain (default 28 = 7d @ 6h)
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

_PREFIX = os.environ.get("BACKUP_PREFIX", "db-backups").strip("/")
_KEEP = int(os.environ.get("BACKUP_KEEP", "28"))


def _r2_client():
    import boto3
    from botocore.config import Config
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT_URL"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
    )


def main() -> int:
    db_url = os.environ.get("DATABASE_URL", "").strip()
    bucket = os.environ.get("R2_BUCKET", "").strip()
    if not db_url:
        print("[backup] FATAL: DATABASE_URL not set", file=sys.stderr)
        return 2
    if not (bucket and os.environ.get("R2_ENDPOINT_URL")):
        print("[backup] FATAL: R2 credentials not set", file=sys.stderr)
        return 2

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    key = f"{_PREFIX}/genly-{ts}.dump"

    with tempfile.TemporaryDirectory() as tmp:
        dump_path = os.path.join(tmp, "db.dump")

        # -Fc = custom format (compressed, restorable with pg_restore,
        # supports selective restore). --no-owner/--no-acl keep the dump
        # portable across roles (e.g. restoring into Cloud SQL later).
        print(f"[backup] {ts} pg_dump → {dump_path}")
        try:
            subprocess.run(
                ["pg_dump", "-Fc", "--no-owner", "--no-acl", "-f", dump_path, db_url],
                check=True,
                capture_output=True,
                text=True,
                timeout=1800,  # 30 min ceiling; a 1.1 GB DB dumps in ~minutes
            )
        except subprocess.CalledProcessError as e:
            print(f"[backup] pg_dump FAILED (exit {e.returncode})", file=sys.stderr)
            print(e.stderr[-2000:], file=sys.stderr)
            return 1
        except FileNotFoundError:
            print("[backup] FATAL: pg_dump not installed on runner", file=sys.stderr)
            return 2

        size_mb = os.path.getsize(dump_path) / (1024 * 1024)
        print(f"[backup] dump size: {size_mb:.2f} MB")

        # Self-verify the dump is real, not an empty shell. A backup you've
        # never inspected is a hope. `pg_restore --list` enumerates the
        # archive's TOC: every table/sequence/index plus a "TABLE DATA"
        # entry per table that actually has rows. We assert there's at least
        # one TABLE DATA entry, and log the count + the tables with data so
        # the run output is auditable.
        try:
            listing = subprocess.run(
                ["pg_restore", "--list", dump_path],
                check=True, capture_output=True, text=True, timeout=120,
            ).stdout
            data_entries = [ln for ln in listing.splitlines() if "TABLE DATA" in ln]
            print(f"[backup] verify: {len(data_entries)} tables with data in the dump")
            for ln in data_entries[:40]:
                # e.g. "1234; 0 0 TABLE DATA public jobs genly"
                parts = ln.split("TABLE DATA", 1)[1].split()
                print(f"[backup]   - {parts[1] if len(parts) > 1 else parts[0]}")
            if not data_entries:
                print("[backup] FATAL: dump has ZERO tables with data — refusing "
                      "to upload an empty backup", file=sys.stderr)
                return 1
        except Exception as e:
            print(f"[backup] FATAL: could not verify dump with pg_restore --list: "
                  f"{type(e).__name__}: {e}", file=sys.stderr)
            return 1

        print(f"[backup] uploading to s3://{bucket}/{key}")

        client = _r2_client()
        client.upload_file(dump_path, bucket, key)
        print(f"[backup] uploaded OK")

    _prune(client, bucket)
    return 0


def _prune(client, bucket: str) -> None:
    """Keep the newest _KEEP dumps under the prefix; delete the rest."""
    try:
        keys = []
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=f"{_PREFIX}/"):
            for obj in page.get("Contents", []):
                if obj["Key"].endswith(".dump"):
                    keys.append((obj["Key"], obj["LastModified"]))
        keys.sort(key=lambda kv: kv[1], reverse=True)  # newest first
        stale = keys[_KEEP:]
        if not stale:
            print(f"[backup] retention: {len(keys)} dumps, none to prune (keep {_KEEP})")
            return
        for k, _ in stale:
            client.delete_object(Bucket=bucket, Key=k)
        print(f"[backup] retention: pruned {len(stale)} old dump(s), kept newest {_KEEP}")
    except Exception as e:
        # Pruning failure must not fail the backup itself — the dump is
        # already safe in R2; worst case storage grows a little.
        print(f"[backup] prune warning (non-fatal): {type(e).__name__}: {e}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
