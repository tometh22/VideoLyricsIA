#!/usr/bin/env python3
"""Bind a private review queue to short-lived, GET-only source-audio URLs.

The staging database transaction is read only. All audio identities must still
match the sealed snapshot; a changed source aborts the entire export.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlparse

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def sign_urls(db, queue: list[dict], provenance: list[dict], *, expiry_seconds: int) -> dict:
    from database import Job
    import storage

    if not queue or len(queue) != len(provenance):
        raise ValueError("queue_and_provenance_count_mismatch")
    receipts = {row.get("task_id"): row for row in provenance}
    if len(receipts) != len(provenance):
        raise ValueError("duplicate_provenance_task")
    expected = {}
    for task in queue:
        receipt = receipts.get(task.get("task_id"))
        if (
            task.get("schema") != "operator-calibration-triage-v1"
            or receipt is None
            or task.get("job_id") != receipt.get("job_id")
            or receipt.get("operator_revision_is_gold") is not False
        ):
            raise ValueError("queue_and_provenance_identity_mismatch")
        identity = (receipt.get("audio_sha256"), receipt.get("audio_revision"))
        previous = expected.setdefault(task["job_id"], identity)
        if previous != identity:
            raise ValueError("conflicting_audio_identity_for_job")
    jobs = {row.job_id: row for row in db.query(Job).filter(
        Job.job_id.in_(sorted(expected)),
    ).all()}
    urls = {}
    for job_id, (audio_hash, revision) in sorted(expected.items()):
        job = jobs.get(job_id)
        if (
            job is None or not job.input_r2_key
            or job.input_audio_sha256 != audio_hash
            or int(job.audio_revision or 0) != int(revision or 0)
        ):
            raise ValueError("source_audio_changed_or_unavailable")
        url = storage.generate_signed_url(job.input_r2_key, expiry_seconds=expiry_seconds)
        if not isinstance(url, str) or urlparse(url).scheme != "https":
            raise ValueError("signed_audio_url_invalid")
        urls[job_id] = url
    queue_sha = hashlib.sha256(json.dumps(
        queue, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    return {
        "schema": "operator-calibration-audio-urls-v1",
        "queue_sha256": queue_sha,
        "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=expiry_seconds)).isoformat(),
        "audio_urls": urls,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--provenance", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expiry-hours", type=int, default=4)
    args = parser.parse_args()
    if not 1 <= args.expiry_hours <= 12:
        parser.error("expiry-hours must be between 1 and 12")
    if ".context" not in args.output.resolve().parts:
        parser.error("private output must remain inside .context")
    queue = _rows(args.queue)
    provenance = _rows(args.provenance)
    from sqlalchemy import text
    from database import SessionLocal
    db = SessionLocal()
    try:
        db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        payload = sign_urls(db, queue, provenance, expiry_seconds=args.expiry_hours * 3600)
    finally:
        db.rollback()
        db.close()
    args.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(args.output.parent, 0o700)
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(payload, output, ensure_ascii=False, sort_keys=True)
        output.write("\n")
    print(json.dumps({"jobs_with_audio": len(payload["audio_urls"]), "expires_at": payload["expires_at"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
