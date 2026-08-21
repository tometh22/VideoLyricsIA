#!/usr/bin/env python3
"""Re-enqueue analysis-only quality jobs for exact persisted revisions.

Dry-run is the default. The command never edits lyric segments and delegates
all writes to the OCC-protected quality worker.
"""
from __future__ import annotations

import argparse
import os


def plan_job(job_id: str) -> dict:
    from database import Job, SessionLocal
    from transcription_quality import build_unsafe_windows, segments_hash

    db = SessionLocal()
    try:
        row = db.query(Job).filter(Job.job_id == job_id).first()
        if row is None:
            raise LookupError("job_not_found")
        segments = [dict(item) for item in (row.segments_json or [])]
        current_quality = dict(row.transcription_quality or {})
        derived = build_unsafe_windows(segments, [])
        windows_by_id = {
            str(item.get("id")): item
            for item in [*(current_quality.get("unsafe_windows") or []), *derived]
            if isinstance(item, dict) and item.get("id")
        }
        return {
            "job_id": job_id,
            "revision": int(row.segments_revision or 0),
            "segments_hash": segments_hash(segments),
            "filename": os.path.basename(row.filename or "audio.mp3"),
            "tenant_id": str(row.tenant_id or ""),
            "windows": list(windows_by_id.values()),
        }
    finally:
        db.close()


def persist_windows(plan: dict) -> None:
    """Persist diagnostics only when the planned snapshot is still current."""
    from database import Job, SessionLocal
    from transcription_quality import segments_hash

    db = SessionLocal()
    try:
        row = db.query(Job).filter(Job.job_id == plan["job_id"]).with_for_update().one()
        if (
            int(row.segments_revision or 0) != plan["revision"]
            or segments_hash(row.segments_json or []) != plan["segments_hash"]
        ):
            raise RuntimeError("stale_snapshot")
        quality = dict(row.transcription_quality or {})
        quality["unsafe_windows"] = plan["windows"]
        for stale_key in (
            "acknowledgement", "quality_fingerprint", "acoustic_evidence",
            "analysis_windows", "retry",
        ):
            quality.pop(stale_key, None)
        row.transcription_quality = quality
        db.commit()
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("job_id")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    plan = plan_job(args.job_id)
    print(
        f"job={plan['job_id']} revision={plan['revision']} "
        f"windows={len(plan['windows'])} mode={'execute' if args.execute else 'dry-run'}"
    )
    if not plan["windows"]:
        print("nothing_to_enqueue")
        return 2
    if not args.execute:
        return 0
    import queue_jobs
    if not queue_jobs.transcription_quality_queue_enabled():
        print("enqueue_failed=disabled")
        return 3
    if not queue_jobs.transcription_quality_rollout_eligible(
        plan["job_id"], plan["tenant_id"],
    ):
        print("enqueue_failed=rollout-excluded")
        return 3
    queue_jobs._init_redis()
    if queue_jobs._redis is None:
        print("enqueue_failed=redis-unavailable")
        return 3
    persist_windows(plan)
    rq_id = queue_jobs.enqueue_transcription_quality(
        plan["job_id"], expected_revision=plan["revision"],
        expected_segments_hash=plan["segments_hash"],
        filename=plan["filename"], tenant_id=plan["tenant_id"],
    )
    print(f"enqueued={rq_id}")
    if not str(rq_id).startswith("transcription-quality:"):
        print(f"enqueue_failed={str(rq_id).split(':', 1)[0]}")
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
