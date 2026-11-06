#!/usr/bin/env python3
"""Extract a blind audio-review queue from verified operator revisions.

The database transaction is REPEATABLE READ and READ ONLY. No lyric or audio
content is printed or written. Output files are private, mode 0600, under a
gitignored .context directory. The queue is triage, never calibration gold.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))


def _records(db, *, operator_id: int, tenant_id: str, limit: int):
    from database import EditorDocument, EditorVersion, Job

    revisions = db.query(
        EditorVersion.job_id, EditorVersion.id, EditorVersion.revision,
    ).filter(
        EditorVersion.created_by == operator_id,
        EditorVersion.tenant_id == tenant_id,
        EditorVersion.reason.in_(("manual", "autosave", "draft", "approve")),
    ).order_by(EditorVersion.job_id, EditorVersion.revision.desc()).all()
    latest = {}
    for job_id, version_id, revision in revisions:
        latest.setdefault(job_id, (version_id, revision))
    if len(latest) > limit:
        raise RuntimeError(f"operator cohort exceeds --limit ({len(latest)} > {limit})")
    job_ids = sorted(latest)
    for offset in range(0, len(job_ids), 25):
        batch = job_ids[offset:offset + 25]
        jobs = {row.job_id: row for row in db.query(Job).filter(Job.job_id.in_(batch)).all()}
        documents = {row.job_id: row for row in db.query(EditorDocument).filter(
            EditorDocument.job_id.in_(batch),
        ).all()}
        first_versions = {row.job_id: row for row in db.query(EditorVersion).filter(
            EditorVersion.job_id.in_(batch), EditorVersion.revision == 0,
        ).all()}
        target_ids = [latest[job_id][0] for job_id in batch]
        target_versions = {row.id: row for row in db.query(EditorVersion).filter(
            EditorVersion.id.in_(target_ids),
        ).all()}
        for job_id in batch:
            job, document = jobs.get(job_id), documents.get(job_id)
            if job is None or document is None:
                yield {"job_id": job_id, "operator_id": operator_id}
                continue
            first = first_versions.get(job_id)
            target = target_versions.get(latest[job_id][0])
            quality = job.transcription_quality or {}
            yield {
                "job_id": job_id,
                "operator_id": operator_id,
                "artist": job.artist,
                "audio_sha256": job.input_audio_sha256,
                "audio_revision": job.audio_revision,
                "current_revision": document.revision,
                "input_r2_key": job.input_r2_key,
                "timing_source": job.timing_source,
                "is_live": (quality.get("metrics") or {}).get("is_live"),
                "original_segments": document.original_segments,
                "current_segments": document.current_segments,
                "edited_segments": target.segments if target else None,
                "machine_evidence": document.machine_evidence,
                "original_version": {
                    "id": first.id, "revision": first.revision,
                    "reason": first.reason, "segments": first.segments,
                    "provenance": first.provenance,
                } if first else {},
                "edited_version": {
                    "id": target.id, "revision": target.revision,
                    "reason": target.reason, "segments": target.segments,
                    "created_by": target.created_by,
                } if target else {},
            }


def _write_private(path: Path, rows) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        for row in rows:
            output.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operator-id", required=True, type=int)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    if args.operator_id < 1 or args.limit < 1 or args.limit > 10000:
        parser.error("operator-id and limit must be positive; limit <= 10000")
    if args.write != bool(args.output_dir):
        parser.error("--write and --output-dir must be supplied together")
    if args.output_dir and ".context" not in args.output_dir.resolve().parts:
        parser.error("private output must be inside a .context directory")

    from sqlalchemy import text
    from database import SessionLocal
    from operator_calibration_triage import build_triage

    db = SessionLocal()
    try:
        db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        result = build_triage(
            _records(db, operator_id=args.operator_id, tenant_id=args.tenant, limit=args.limit),
            secret=os.environ.get("QUALITY_LEARNING_HMAC_KEY", ""),
        )
    finally:
        db.rollback()
        db.close()
    summary = result["summary"]
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    if args.write:
        output_dir = args.output_dir.resolve()
        output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(output_dir, 0o700)
        _write_private(output_dir / "blind_queue.jsonl", result["queue"])
        _write_private(output_dir / "sealed_provenance.jsonl", result["provenance"])
        _write_private(output_dir / "summary.jsonl", [summary])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
