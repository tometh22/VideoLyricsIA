#!/usr/bin/env python3
"""Prepare a safe, auditable repair for an exported editor document.

This tool never mutates a database. It reads a JSON response copied from
GET /editor/{job_id}, reports timing anomalies, and optionally writes a
chronologically ordered candidate plus a side-by-side backup. Human review
is required before applying the candidate to a live job.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

# Allow direct execution from the repository root as well as from this
# directory: the timing helper lives one level above ``scripts``.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from segment_timing import sort_segments_chronologically, timing_anomalies


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path, help="exported GET /editor JSON")
    parser.add_argument("--output", type=Path, help="candidate JSON path")
    parser.add_argument("--write", action="store_true", help="write candidate and backup")
    args = parser.parse_args()

    payload = json.loads(args.input.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        segments = payload
        root = None
    elif isinstance(payload, dict) and isinstance(payload.get("segments"), list):
        segments = payload["segments"]
        root = dict(payload)
    else:
        raise SystemExit("input must be an editor response or a segment array")

    anomalies = timing_anomalies(segments)
    ordered = sort_segments_chronologically(segments)
    print(json.dumps({
        "job_id": root.get("job_id") if root else None,
        "revision": root.get("revision") if root else None,
        "before": len(segments),
        "after": len(ordered),
        "anomalies": anomalies,
        "changed_order": [s.get("_id") for s in ordered] != [s.get("_id") for s in segments],
    }, ensure_ascii=False, indent=2))

    if not args.write:
        print("dry-run: no file written")
        return 0
    if not args.output:
        raise SystemExit("--write requires --output")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    backup = args.output.with_suffix(args.output.suffix + ".before")
    shutil.copy2(args.input, backup)
    candidate = dict(root) if root is not None else ordered
    if root is not None:
        candidate["segments"] = ordered
        candidate["repair"] = {
            "kind": "stable_chronological_sort",
            "source_revision": root.get("revision"),
            "requires_human_review": True,
        }
    args.output.write_text(json.dumps(candidate, ensure_ascii=False, indent=2) + "\n")
    print(f"wrote candidate={args.output} backup={backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
