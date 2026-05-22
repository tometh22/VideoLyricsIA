#!/usr/bin/env python3
"""One-off: migrate approved-but-stranded Delivery rows from staging
into the prod UMG portal.

Why this exists
---------------
The operator published 9 approved jobs from the staging admin (clicked
"Enviar a UMG"). Those Delivery rows landed in the staging DB, and the
5 R2 files for each landed in the `videolyricsia-staging` bucket. The
public portal at umg.genly.pro is hardcoded (vercel.json) to read prod
backend → prod DB, so those deliveries never appeared.

This script:
  1. Reads job metadata from the staging Postgres (artist, song,
     tenant, frame_size, file_types).
  2. Copies the 5 R2 files for each job from staging bucket to prod
     bucket, preserving the `{tenant}/{job_id}/{filename}` key shape
     the portal expects.
  3. Inserts a Delivery row in prod DB with snapshot fields, so the
     portal listing surfaces it on next refresh.

Required env vars (run before invoking the script):
    SOURCE_DATABASE_URL      staging Postgres (use PUBLIC URL if running from laptop)
    DEST_DATABASE_URL        prod Postgres (use PUBLIC URL if running from laptop)
    DEST_ADMIN_USER_ID       integer user id in prod (for added_by_user_id FK)
    R2_ACCESS_KEY_ID         shared R2 access key (both envs use same bucket)
    R2_SECRET_ACCESS_KEY     shared R2 secret
    R2_ENDPOINT_URL          shared R2 endpoint
    R2_BUCKET                shared bucket (typically genly-deliverables)

Note on R2 buckets: staging and prod share the same R2 bucket
(genly-deliverables). The files for staging deliveries are already
in the bucket at `{tenant}/{job_id}/{filename}` — the portal just
needs the Delivery row in the prod DB to surface them. No file
copy is performed.

Usage:
    # Step 1 — dry run (READ ONLY). Prints what would be migrated.
    python migrate_deliveries_staging_to_prod.py plan

    # Step 2 — actually do it. Run after the plan looks correct.
    python migrate_deliveries_staging_to_prod.py migrate

The list of job_ids to migrate lives in `_TARGET_JOB_PREFIXES` below.
Edit that list and re-run if you need to migrate a different set.
"""

import os
import re
import sys
import json
from datetime import datetime, timezone

# Mirror storage._safe_filename — tenant_ids with @ or . (typical when
# tenant = user email) get sanitised when files are written to R2.
# The portal listing uses the raw tenant in the key, but the actual
# objects live under the sanitised name. Without this, every Delivery
# for an email-style tenant looked "missing" in the dry run.
_KEY_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def _safe_filename(name: str) -> str:
    base = os.path.basename(name or "")
    cleaned = _KEY_SAFE.sub("_", base).strip(".")
    return cleaned[:200] or "file"


def _r2_key_for_delivery_safe(tenant: str, job_id: str, filename: str) -> str:
    return f"{_safe_filename(tenant)}/{_safe_filename(job_id)}/{_safe_filename(filename)}"

# Short 8-char prefixes of the 9 jobs the operator confirmed should be
# in the UMG portal. The script resolves these to full 12-char job_ids
# via a LIKE query against staging.jobs.job_id.
_TARGET_JOB_PREFIXES = [
    "2650bca4",  # Intoxicados — Reggae Para Los Amigos (omg)
    "b23d6cc3",  # Intoxicados — Don Electrón (omg)
    "c94c480f",  # Los Abuelos De La Nada — Lunes Por La Madrugada (omg)
    "411f5156",  # Intoxicados — De La Guitarra v2 (omg, 14/5 4:02pm)
    "284a63a8",  # Babasónicos — Como Eran Las Cosas (omg)
    "3147ae7b",  # Enanitos Verdes — Amigos (omg)
    "2be5d534",  # Bersuit Vergarabat — La Soledad (omg) — artist field may say "La Soledad"
    "98944db8",  # Intoxicados — De La Guitarra v1 (omg, 14/5 2:50pm) — kept per operator
    "7cc8e219",  # Enanitos Verdes — Mi Primer Día Sin Ti (tomas@epical) — missing R2 files
    # Second batch, approved on 2026-05-15 after the first migration:
    "63e89de1",  # La Soledad — Bersuit Vergarabat (tomas@epical, 15/5 4:41pm)
    "bd392eb7",  # Los Abuelos de la Nada — Costumbres Argentinas (tomas@epical, 15/5 12:41pm)
    # Tercer batch (2026-05-21): variante corregida sin movimiento de cámara, enviada desde staging.
    "8044c9272ded",  # Los Abuelos de la Nada — Costumbres Argentinas (variante sin mov. de cámara)
]

# Match the file map the portal expects. Keys are file_type identifiers
# (matching _DELIVERY_FILE_TYPES in main.py); values are the filename in
# the R2 key `{tenant}/{job_id}/{filename}`.
_FILES = {
    "umg_master": "umg_master.mov",
    "umg_short":  "umg_short.mov",
    "video":      "lyric_video.mp4",
    "short":      "short.mp4",
    "thumbnail":  "thumbnail.jpg",
}
_FILE_TYPES_LIST = ["umg_master", "video", "umg_short", "short", "thumbnail"]


def _need(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        print(f"ERROR: env var {name} not set", file=sys.stderr)
        sys.exit(2)
    return v


def _make_pg(url: str):
    """Create a SQLAlchemy session bound to `url`. Lazy import so the
    script works in dry-run when only some vars are set."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    eng = create_engine(url, pool_pre_ping=True)
    return sessionmaker(bind=eng, autocommit=False, autoflush=False)()


def _make_s3(access_key: str, secret_key: str, endpoint: str):
    import boto3
    from botocore.config import Config
    return boto3.client(
        "s3",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        endpoint_url=endpoint,
        config=Config(
            signature_version="s3v4",
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


def _resolve_jobs(staging_db):
    """Return [{job_id, artist, song_title, tenant_id, frame_size}] for
    each prefix in _TARGET_JOB_PREFIXES that resolves to exactly one
    staging job."""
    from sqlalchemy import text
    rows = []
    for pfx in _TARGET_JOB_PREFIXES:
        q = text(
            "SELECT job_id, artist, song_title, tenant_id, umg_spec "
            "FROM jobs WHERE job_id LIKE :pfx"
        )
        matches = staging_db.execute(q, {"pfx": pfx + "%"}).fetchall()
        if len(matches) == 0:
            print(f"  SKIP {pfx}: no matching job in staging")
            continue
        if len(matches) > 1:
            print(f"  SKIP {pfx}: ambiguous ({len(matches)} matches) — narrow prefix")
            continue
        r = matches[0]
        umg_spec = r[4] or {}
        if isinstance(umg_spec, str):
            try:
                umg_spec = json.loads(umg_spec)
            except Exception:
                umg_spec = {}
        rows.append({
            "job_id": r[0],
            "artist": r[1] or "",
            "song_title": r[2] or "",
            "tenant_id": r[3] or "",
            "frame_size": umg_spec.get("frame_size"),
        })
    return rows


def _check_source_files(s3_src, bucket: str, jobs: list) -> list:
    """For each job, HEAD the 5 expected R2 keys. Returns a list of
    {job, missing: [file_types]}. Empty missing means OK to migrate."""
    out = []
    for j in jobs:
        missing = []
        for ft in _FILE_TYPES_LIST:
            key = _r2_key_for_delivery_safe(j['tenant_id'], j['job_id'], _FILES[ft])
            try:
                s3_src.head_object(Bucket=bucket, Key=key)
            except Exception:
                missing.append(ft)
        out.append({"job": j, "missing": missing})
    return out


def _copy_file(s3_src, src_bucket: str, s3_dst, dst_bucket: str, key: str):
    """Stream a single R2 key from source to dest. Works across R2 accounts
    by downloading + uploading; uses CopySource if both clients point at
    the same endpoint (faster). Same-key on both sides."""
    src_endpoint = getattr(s3_src.meta, "endpoint_url", None)
    dst_endpoint = getattr(s3_dst.meta, "endpoint_url", None)
    if src_endpoint and src_endpoint == dst_endpoint:
        # Same R2 account → server-side copy avoids the round-trip.
        s3_dst.copy_object(
            Bucket=dst_bucket,
            Key=key,
            CopySource={"Bucket": src_bucket, "Key": key},
        )
        return "server-side"
    # Different endpoints → stream through this process.
    obj = s3_src.get_object(Bucket=src_bucket, Key=key)
    body = obj["Body"]
    s3_dst.upload_fileobj(body, dst_bucket, key)
    return "streamed"


def _create_prod_delivery(prod_db, j: dict, admin_user_id: int):
    """INSERT a Delivery row in prod for this staging job, with snapshot
    fields. Idempotent: if a row with the same job_id already exists
    (and is not removed), we leave it alone and report the existing row."""
    from sqlalchemy import text
    existing = prod_db.execute(
        text(
            "SELECT id, label FROM deliveries "
            "WHERE job_id = :job_id AND removed_at IS NULL"
        ),
        {"job_id": j["job_id"]},
    ).fetchone()
    if existing:
        return ("existed", existing[0], existing[1])

    # Match _compute_default_delivery_label: first active delivery for the
    # (artist, song) tuple is "Renderizado"; subsequent ones become
    # "Opción N" where N = active count + 1.
    cnt = prod_db.execute(
        text(
            "SELECT COUNT(*) FROM deliveries "
            "WHERE artist_snapshot = :artist AND song_title_snapshot = :song "
            "AND removed_at IS NULL"
        ),
        {"artist": j["artist"], "song": j["song_title"]},
    ).scalar()
    label = "Renderizado" if cnt == 0 else f"Opción {cnt + 1}"

    file_types_json = json.dumps(_FILE_TYPES_LIST)
    now = datetime.now(timezone.utc)
    prod_db.execute(
        text(
            "INSERT INTO deliveries "
            "(job_id, label, file_types, artist_snapshot, song_title_snapshot, "
            "tenant_snapshot, frame_size_snapshot, added_by_user_id, added_at) "
            "VALUES "
            "(:job_id, :label, CAST(:file_types AS JSONB), :artist, :song, "
            ":tenant, :frame_size, :uid, :added_at)"
        ),
        {
            "job_id": j["job_id"],
            "label": label,
            "file_types": file_types_json,
            "artist": j["artist"],
            "song": j["song_title"],
            "tenant": j["tenant_id"],
            "frame_size": j["frame_size"],
            "uid": admin_user_id,
            "added_at": now,
        },
    )
    return ("inserted", None, label)


def cmd_plan():
    """Read-only: resolve jobs, validate R2 files exist in the shared bucket."""
    staging_db = _make_pg(_need("SOURCE_DATABASE_URL"))
    s3 = _make_s3(
        _need("R2_ACCESS_KEY_ID"),
        _need("R2_SECRET_ACCESS_KEY"),
        _need("R2_ENDPOINT_URL"),
    )
    bucket = _need("R2_BUCKET")

    print("Resolving job prefixes against staging DB...")
    jobs = _resolve_jobs(staging_db)
    print(f"\nResolved {len(jobs)} jobs:")
    for j in jobs:
        print(
            f"  {j['job_id']}  {j['artist'][:30]:30s}  "
            f"{(j['song_title'] or '')[:30]:30s}  tenant={j['tenant_id']}  "
            f"frame={j['frame_size']}"
        )

    print(f"\nChecking R2 files in bucket '{bucket}' (shared staging+prod)...")
    checks = _check_source_files(s3, bucket, jobs)
    ready = [c for c in checks if not c["missing"]]
    partial = [c for c in checks if c["missing"]]
    for c in checks:
        if c["missing"]:
            print(f"  ⚠ {c['job']['job_id']}: missing {c['missing']}")
        else:
            print(f"  ✓ {c['job']['job_id']}: all 5 files present")
    print(f"\nReady to migrate: {len(ready)} / {len(checks)}")
    if partial:
        print(f"WARNING: {len(partial)} job(s) have missing source files. "
              f"They will be SKIPPED in the migrate phase.")
    print("\nIf this looks correct, re-run with 'migrate' to apply.")


def cmd_migrate():
    """Apply: copy R2 files staging→prod, insert Delivery rows in prod."""
    staging_db = _make_pg(_need("SOURCE_DATABASE_URL"))
    prod_db = _make_pg(_need("DEST_DATABASE_URL"))
    s3_src = _make_s3(
        _need("SOURCE_R2_ACCESS_KEY_ID"),
        _need("SOURCE_R2_SECRET_ACCESS_KEY"),
        _need("SOURCE_R2_ENDPOINT_URL"),
    )
    s3_dst = _make_s3(
        _need("DEST_R2_ACCESS_KEY_ID"),
        _need("DEST_R2_SECRET_ACCESS_KEY"),
        _need("DEST_R2_ENDPOINT_URL"),
    )
    src_bucket = _need("SOURCE_R2_BUCKET")
    dst_bucket = _need("DEST_R2_BUCKET")
    admin_user_id = int(_need("DEST_ADMIN_USER_ID"))

    jobs = _resolve_jobs(staging_db)
    if not jobs:
        print("No jobs resolved. Nothing to do.")
        return

    print(f"Migrating {len(jobs)} jobs...\n")
    summary = {"copied": 0, "skipped_missing": 0, "delivery_inserted": 0,
               "delivery_existed": 0, "copy_errors": 0}
    for j in jobs:
        print(f"--- {j['job_id']} {j['artist']} — {j['song_title']} ---")
        # Phase A: copy 5 files
        ok = True
        for ft in _FILE_TYPES_LIST:
            key = _r2_key_for_delivery_safe(j['tenant_id'], j['job_id'], _FILES[ft])
            try:
                # Skip if already in dest (idempotent re-runs).
                try:
                    s3_dst.head_object(Bucket=dst_bucket, Key=key)
                    print(f"  {ft}: already in prod R2 — skip")
                    continue
                except Exception:
                    pass
                mode = _copy_file(s3_src, src_bucket, s3_dst, dst_bucket, key)
                print(f"  {ft}: copied ({mode})")
                summary["copied"] += 1
            except Exception as e:
                print(f"  {ft}: ERROR {e!r}")
                summary["copy_errors"] += 1
                ok = False
                break
        if not ok:
            summary["skipped_missing"] += 1
            print("  → skipped delivery row (file copy failed)")
            continue

        # Phase B: insert Delivery row in prod DB
        action, existing_id, label = _create_prod_delivery(prod_db, j, admin_user_id)
        if action == "existed":
            summary["delivery_existed"] += 1
            print(f"  delivery: already exists (id={existing_id}, label={label})")
        else:
            summary["delivery_inserted"] += 1
            print(f"  delivery: INSERTED (label={label})")

    prod_db.commit()
    print("\n=== summary ===")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    print("\nPortal at umg.genly.pro should list these on next refresh.")


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("plan", "migrate"):
        print("Usage: migrate_deliveries_staging_to_prod.py {plan|migrate}",
              file=sys.stderr)
        sys.exit(2)
    if sys.argv[1] == "plan":
        cmd_plan()
    else:
        cmd_migrate()


if __name__ == "__main__":
    main()
