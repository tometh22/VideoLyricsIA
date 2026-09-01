#!/usr/bin/env python3
"""Materialize tenant-private transcription training pairs as JSONL.

The command is SELECT-only.  It never repairs or estimates missing history;
incomplete jobs are exported with explicit issues and make ``--require-complete``
fail.  The output contains raw lyrics and must remain in approved private
storage.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from database import AuditLog, EditorDocument, EditorVersion, Job, SessionLocal  # noqa: E402
from training_corpus import materialize_training_pair  # noqa: E402


def _latest_eligible_jobs(db, limit: int) -> list[Job]:
    approved_jobs = (
        db.query(EditorVersion.job_id)
        .filter(EditorVersion.is_approved.is_(True))
        .distinct()
        .subquery()
    )
    return (
        db.query(Job)
        .join(approved_jobs, approved_jobs.c.job_id == Job.job_id)
        .filter(Job.machine_snapshot_required.is_(True))
        .order_by(Job.created_at.desc(), Job.job_id.desc())
        .limit(limit)
        .all()
    )


def _required_export_failed(manifest: dict, requested_rows: int) -> bool:
    return (
        int(manifest.get("rows") or 0) != int(requested_rows)
        or int(manifest.get("incomplete_rows") or 0) != 0
    )


def _write_private(path: Path, payload: bytes) -> None:
    """Write tenant-private bytes only after the descriptor is mode 0600."""
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    try:
        # O_CREAT's mode does not change a pre-existing permissive file.
        # Tighten the open descriptor before a single new byte is written.
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(payload)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _job_audits(db, job_id: str) -> list[AuditLog]:
    query = db.query(AuditLog).filter(
        AuditLog.action == "lyrics.segments_diff",
    )
    # JSON path comparison works on deployed Postgres and on the SQLite test
    # engine used by local verification.
    try:
        query = query.filter(AuditLog.detail["job_id"].as_string() == job_id)
        return query.order_by(AuditLog.created_at.asc(), AuditLog.id.asc()).all()
    except (AttributeError, NotImplementedError):
        return [
            row for row in query.order_by(AuditLog.created_at.asc(), AuditLog.id.asc()).all()
            if str((row.detail or {}).get("job_id") or "") == job_id
        ]


def export_pairs(db, jobs: list[Job]) -> list[dict]:
    rows = []
    for job in jobs:
        document = db.query(EditorDocument).filter(
            EditorDocument.job_id == job.job_id,
        ).first()
        versions = db.query(EditorVersion).filter(
            EditorVersion.job_id == job.job_id,
        ).order_by(EditorVersion.revision.asc(), EditorVersion.created_at.asc()).all()
        rows.append(materialize_training_pair(
            job=job,
            document=document,
            versions=versions,
            audits=_job_audits(db, job.job_id),
        ))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--job-id", action="append", default=[])
    parser.add_argument("--latest", type=int, default=5)
    parser.add_argument("--require-complete", action="store_true")
    args = parser.parse_args()
    if args.latest < 1 or args.latest > 1000:
        parser.error("--latest must be between 1 and 1000")

    db = SessionLocal()
    requested_rows = args.latest
    try:
        if args.job_id:
            unique_ids = list(dict.fromkeys(str(value) for value in args.job_id))
            requested_rows = len(unique_ids)
            by_id = {
                row.job_id: row for row in db.query(Job).filter(
                    Job.job_id.in_(unique_ids),
                ).all()
            }
            missing = [job_id for job_id in unique_ids if job_id not in by_id]
            if missing:
                print(json.dumps({"error": "jobs_not_found", "job_ids": missing}))
                return 2
            jobs = [by_id[job_id] for job_id in unique_ids]
        else:
            jobs = _latest_eligible_jobs(db, args.latest)
        rows = export_pairs(db, jobs)
    finally:
        db.close()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    encoded_rows = [
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for row in rows
    ]
    payload = ("\n".join(encoded_rows) + ("\n" if encoded_rows else "")).encode("utf-8")
    _write_private(args.output, payload)
    digest = hashlib.sha256(payload).hexdigest()
    manifest = {
        "schema": "transcription-training-export-manifest-v1",
        "rows": len(rows),
        "requested_rows": requested_rows,
        "complete_rows": sum(bool(row.get("complete")) for row in rows),
        "incomplete_rows": sum(not bool(row.get("complete")) for row in rows),
        "job_ids": [row.get("job_id") for row in rows],
        "jsonl_sha256": digest,
        "contains_raw_lyrics": True,
        "contains_audio": False,
    }
    manifest_path = args.output.with_suffix(args.output.suffix + ".manifest.json")
    _write_private(
        manifest_path,
        (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    print(json.dumps(manifest, separators=(",", ":")))
    if args.require_complete and _required_export_failed(manifest, requested_rows):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
