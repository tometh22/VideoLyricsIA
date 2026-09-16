#!/usr/bin/env python3
"""Read-only LID autopsy for one batch campaign.

The report intentionally contains metadata and language labels only; lyric
text never leaves the job row.  ``lid_persistence_failure`` is actionable:
the final persisted lines contain one supported-language signal while the
stored quality metric is still unknown.  The two abstention classes are safe
and must not be replaced with a Spanish default.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter


BACKEND_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, BACKEND_ROOT)


def collect(db, campaign_id: str) -> list[dict]:
    from database import Job
    from transcription_language import diagnose_language_state

    rows: list[dict] = []
    jobs = (
        db.query(Job)
        .filter(Job.campaign_id == campaign_id)
        .order_by(Job.created_at.asc())
        .all()
    )
    for job in jobs:
        quality = job.transcription_quality if isinstance(
            job.transcription_quality, dict,
        ) else {}
        metrics = quality.get("metrics") if isinstance(
            quality.get("metrics"), dict,
        ) else {}
        persisted = str(metrics.get("language") or "unknown")
        diagnostic = diagnose_language_state(job.segments_json or [], persisted)
        rows.append({
            "job_id": job.job_id,
            "artist": job.artist,
            "title": job.song_title or job.filename,
            "job_status": job.status,
            "quality_analysis_status": quality.get("analysis_status"),
            **diagnostic,
        })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument(
        "--all", action="store_true",
        help="include known-language rows (default: unknown/abstention rows only)",
    )
    args = parser.parse_args()

    from database import SessionLocal
    db = SessionLocal()
    try:
        rows = collect(db, args.campaign_id)
        # No commit: this script is an operationally read-only audit.
        db.rollback()
    finally:
        db.close()
    counts = Counter(row["classification"] for row in rows)
    output_rows = rows if args.all else [
        row for row in rows if row["classification"] != "known"
    ]
    print(json.dumps({
        "schema": "batch-language-autopsy-v1",
        "campaign_id": args.campaign_id,
        "songs": len(rows),
        "counts": dict(sorted(counts.items())),
        "rows": output_rows,
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if counts.get("lid_persistence_failure", 0) else 0


if __name__ == "__main__":
    raise SystemExit(main())
