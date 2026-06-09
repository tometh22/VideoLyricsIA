#!/usr/bin/env python3
"""One-off remediation for the stale-ProRes-after-edit bug (2026-06-09).

Context
-------
Before the run_edit_pipeline fix, an edit re-rendered lyric_video.mp4 but left
the lazy ProRes derivatives (umg_master.mov / umg_short.mov) pointing at the
PRE-EDIT cut: the stale .mov survived on disk and s3_keys["umg_master"] kept
the old R2 object. Result: MP4 download = new cut, ProRes download = old cut.

This script invalidates that stale cache for an already-affected job so the
next /download/{id}/umg_master re-transcodes from the (fresh) lyric_video.mp4.
The MP4 in R2 (s3_keys["video"]) is already the new cut, and ensure_prores_exists
pulls its source from that key — so clearing the cache is sufficient; no
re-render is needed.

Safe by design:
  - DRY RUN by default. Pass --apply to mutate.
  - Only removes umg_master / umg_short from s3_keys (MP4/short/thumb untouched).
  - The prior .mov was already archived as {key}.vN by the edit's snapshot,
    so the rollback path is preserved even though we drop the live key.
  - Optionally re-warms the ProRes so the customer gets an instant 302.

Usage (run where prod DATABASE_URL + R2 env are available, e.g. Railway shell):
    python scripts/fix_stale_prores.py 3d37e9b24c34            # dry run
    python scripts/fix_stale_prores.py 3d37e9b24c34 --apply
    python scripts/fix_stale_prores.py 3d37e9b24c34 --apply --rewarm
"""

import argparse
import os
import sys

# Make backend importable when run as scripts/fix_stale_prores.py
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_PRORES_FILES = {
    "umg_master": "umg_master.mov",
    "umg_short": "umg_short.mov",
}


def main() -> int:
    ap = argparse.ArgumentParser(description="Invalidate stale ProRes cache for a job.")
    ap.add_argument("job_id", help="Job id (e.g. 3d37e9b24c34)")
    ap.add_argument("--apply", action="store_true", help="Actually mutate (default: dry run)")
    ap.add_argument("--rewarm", action="store_true", help="Re-enqueue ProRes prewarm after invalidation")
    args = ap.parse_args()

    from database import SessionLocal
    from jobs import get_job_model
    import storage

    db = SessionLocal()
    try:
        job = get_job_model(db, args.job_id)
        if job is None:
            print(f"[ERR] job {args.job_id} not found")
            return 1
        s3_keys = dict(job.s3_keys or {})
        tenant_id = job.tenant_id
        status = job.status
        delivery_profile = job.delivery_profile
    finally:
        db.close()

    print(f"job={args.job_id} status={status} delivery_profile={delivery_profile} tenant={tenant_id}")
    print(f"current s3_keys: {sorted(s3_keys.keys())}")
    video_key = s3_keys.get("video")
    print(f"  video (ProRes source) key: {video_key or '<MISSING — cannot re-transcode!>'}")

    targets = [ft for ft in _PRORES_FILES if ft in s3_keys]
    if not targets:
        print("[OK] no umg_master/umg_short keys present — nothing to invalidate.")
        # Still report local-disk staleness below.

    # Reuse the canonical paths so we look in the exact dir the download path
    # serves from (and use the same .mov filenames).
    from prores import OUTPUTS_DIR
    job_dir = os.path.join(OUTPUTS_DIR, args.job_id)

    # 1. Local stale .mov files
    for ft, fname in _PRORES_FILES.items():
        local = os.path.join(job_dir, fname)
        if os.path.exists(local):
            print(f"[local] stale {fname} present ({os.path.getsize(local)} bytes)"
                  + (" -> WOULD DELETE" if not args.apply else " -> DELETING"))
            if args.apply:
                try:
                    os.unlink(local)
                except OSError as e:
                    print(f"  [warn] could not delete {local}: {e}")

    # 2. R2 keys
    for ft in targets:
        print(f"[s3_keys] {ft}={s3_keys[ft]}"
              + (" -> WOULD REMOVE from job.s3_keys" if not args.apply else " -> REMOVING"))

    if args.apply and targets:
        from jobs import remove_s3_keys
        ok = remove_s3_keys(args.job_id, targets)
        print(f"[s3_keys] remove_s3_keys -> {ok}")

    if args.rewarm:
        if not args.apply:
            print("[rewarm] (dry run) WOULD enqueue prores prewarm for umg_master + umg_short")
        else:
            try:
                from queue_jobs import enqueue_prores_prewarm
                enqueue_prores_prewarm(args.job_id, "umg_master")
                enqueue_prores_prewarm(args.job_id, "umg_short")
                print("[rewarm] enqueued prores prewarm for umg_master + umg_short")
            except Exception as e:
                print(f"[rewarm] [warn] enqueue skipped: {e}")

    if not args.apply:
        print("\nDRY RUN — re-run with --apply (add --rewarm to pre-warm). "
              "After applying, the next ProRes download re-transcodes the fresh cut.")
    else:
        print("\nDONE. Tell the customer to re-download the ProRes master — "
              "it will now serve the latest edit (first hit may take ~60-120 s "
              "if not pre-warmed).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
