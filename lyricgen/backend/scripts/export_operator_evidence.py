#!/usr/bin/env python3
"""Export signed, server-clocked editor effort for one benchmark output."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import timezone
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from evidence_attestation import sign_artifact, write_json_exclusive  # noqa: E402
from evidence_attestation import lyric_snapshot_hash  # noqa: E402


def build_operator_evidence(rows: list[Any], *, case_id: str, system: str,
                            job_id: str, revision: int, snapshot_sha256: str,
                            pipeline_release: str, config_sha256: str,
                            pipeline_config_fingerprint: str,
                            scored_segments_sha256: str) -> dict:
    """Derive active time from consecutive server timestamps, never the UI clock."""
    candidates: list[Any] = []
    for row in rows:
        properties = dict(getattr(row, "properties", None) or {})
        created_at = getattr(row, "created_at", None)
        if (
            getattr(row, "name", None) != "editor_activity_heartbeat"
            or getattr(row, "job_id", None) != job_id
            or created_at is None
            or properties.get("pipeline_release") != pipeline_release
            or properties.get("pipeline_config_fingerprint")
            != pipeline_config_fingerprint
        ):
            continue
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        candidates.append((row, created_at.astimezone(timezone.utc), properties))
    terminal = [item for item in candidates if (
        item[2].get("revision") == revision
        and item[2].get("snapshot_sha256") == snapshot_sha256
    )]
    terminal_sessions = {
        (getattr(row, "user_id", None), props.get("session_id"))
        for row, _at, props in terminal
    }
    if len(terminal_sessions) != 1 or None in next(iter(terminal_sessions), (None, None)):
        raise ValueError("final snapshot must belong to one operator session")
    operator_id, session_id = next(iter(terminal_sessions))
    terminal_at = max(at for _row, at, _props in terminal)
    # Include the whole authenticated editor session up to the final snapshot,
    # crossing autosave revisions instead of undercounting only the last one.
    accepted = [item for item in candidates if (
        getattr(item[0], "user_id", None) == operator_id
        and item[2].get("session_id") == session_id
        and item[1] <= terminal_at
    )]
    if len(accepted) < 2:
        raise ValueError("at least two matching server heartbeats are required")
    accepted.sort(key=lambda item: (item[1], int(getattr(item[0], "id", 0))))
    sequences = [int(props.get("activity_seq", -1)) for _row, _at, props in accepted]
    if sequences != list(range(1, len(sequences) + 1)):
        raise ValueError("heartbeat sequence must start at 1 and be contiguous")
    revisions = [int(props.get("revision", -1)) for _row, _at, props in accepted]
    if any(right < left for left, right in zip(revisions, revisions[1:])):
        raise ValueError("editor revisions must be monotonic")
    active_seconds = 0.0
    for (_left_row, left_at, _left_props), (_right_row, right_at, _right_props) in zip(
        accepted, accepted[1:],
    ):
        gap = (right_at - left_at).total_seconds()
        # The browser emits every 15s. Larger gaps are idle/offline and do not
        # contribute; shorter gaps are bounded to resist duplicate floods.
        if 0 < gap <= 20:
            active_seconds += gap
    if active_seconds <= 0:
        raise ValueError("no contiguous active heartbeat interval found")
    event_ids = [str(getattr(row, "id")) for row, _at, _props in accepted]
    return {
        "schema": "server-editor-session-evidence-v1",
        "source": "server_product_events_v1",
        "case_id": case_id,
        "system": system,
        "job_id": job_id,
        "revision": revision,
        "snapshot_sha256": snapshot_sha256,
        "scored_segments_sha256": scored_segments_sha256,
        "operator_id": str(operator_id),
        "session_id": str(session_id),
        "pipeline_release": pipeline_release,
        "pipeline_config_fingerprint": pipeline_config_fingerprint,
        "config_sha256": config_sha256,
        "active_minutes": round(active_seconds / 60.0, 6),
        "event_ids": event_ids,
        "revision_transitions": [
            {"revision": int(props["revision"]),
             "snapshot_sha256": str(props["snapshot_sha256"])}
            for _row, _at, props in accepted
        ],
        "first_server_timestamp": accepted[0][1].isoformat(),
        "last_server_timestamp": accepted[-1][1].isoformat(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--system", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--revision", required=True, type=int)
    parser.add_argument("--snapshot-sha256", required=True)
    parser.add_argument("--pipeline-release", required=True)
    parser.add_argument("--config-sha256", required=True)
    parser.add_argument("--pipeline-config-fingerprint", required=True)
    parser.add_argument("--scored-output", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    private_key = os.environ.get("BENCHMARK_OPERATOR_EVIDENCE_PRIVATE_KEY") or ""
    key_id = os.environ.get("BENCHMARK_OPERATOR_EVIDENCE_KEY_ID") or ""
    if not private_key or not key_id:
        parser.error("operator evidence private key and key ID are required")

    from database import ProductEvent, SessionLocal

    db = SessionLocal()
    try:
        rows = db.query(ProductEvent).filter(
            ProductEvent.name == "editor_activity_heartbeat",
            ProductEvent.job_id == args.job_id,
        ).order_by(ProductEvent.created_at.asc()).all()
    finally:
        db.close()
    try:
        scored_output = json.loads(args.scored_output.read_text(encoding="utf-8"))
        scored_hash = lyric_snapshot_hash(
            scored_output.get("segments") if isinstance(scored_output, dict) else None,
            include_event_type=True,
        )
        artifact = build_operator_evidence(
            rows, case_id=args.case_id, system=args.system,
            job_id=args.job_id, revision=args.revision,
            snapshot_sha256=args.snapshot_sha256,
            pipeline_release=args.pipeline_release,
            config_sha256=args.config_sha256,
            pipeline_config_fingerprint=args.pipeline_config_fingerprint,
            scored_segments_sha256=scored_hash,
        )
        artifact = sign_artifact(artifact, private_key, key_id)
        write_json_exclusive(args.output, artifact)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
