#!/usr/bin/env python3
"""Backfill approved editor snapshots into correction learning.

Dry-run is the default. ``--apply`` still routes through the isolated quality
queue and never writes observations from the API/CLI process.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))


def collect_candidates(db, limit: int) -> list[tuple[str, str, int]]:
    """Return only current approved revisions absent from analytics."""
    from database import CorrectionObservation, EditorDocument, EditorVersion
    versions = db.query(EditorVersion).filter(
        EditorVersion.is_approved.is_(True),
    ).order_by(EditorVersion.created_at.asc()).limit(limit).all()
    current_revisions = dict(db.query(
        EditorDocument.job_id, EditorDocument.revision,
    ).filter(
        EditorDocument.job_id.in_([row.job_id for row in versions]),
    ).all()) if versions else {}
    existing_versions = {
        value for value, in db.query(
            CorrectionObservation.approved_version_id,
        ).filter(
            CorrectionObservation.approved_version_id.in_([row.id for row in versions])
        ).all()
    } if versions else set()
    return [
        (row.job_id, row.id, row.revision)
        for row in versions
        if row.id not in existing_versions
        and int(current_revisions.get(row.job_id, -1)) == int(row.revision)
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=500)
    args = parser.parse_args()
    if args.limit < 1 or args.limit > 10000:
        parser.error("--limit must be between 1 and 10000")

    from database import SessionLocal
    from queue_jobs import enqueue_correction_learning
    db = SessionLocal()
    try:
        candidates = collect_candidates(db, args.limit)
    finally:
        db.close()

    print(f"mode={'APPLY' if args.apply else 'DRY-RUN'} candidates={len(candidates)}")
    enqueued = failed = 0
    for job_id, version_id, revision in candidates:
        if not args.apply:
            print(f"would enqueue job={job_id} version={version_id} revision={revision}")
            continue
        try:
            enqueue_correction_learning(
                job_id, version_id,
                source_confidence="legacy_unverified",
            )
            enqueued += 1
        except Exception as exc:
            failed += 1
            print(f"failed job={job_id}: {exc}", file=sys.stderr)
    print(f"enqueued={enqueued} failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
