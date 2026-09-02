#!/usr/bin/env python3
"""Create a safe repair candidate from a delivery preflight manifest."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from delivery_repair_agent import repair_delivery_manifest  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Genly delivery repair agent")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", "-o", type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    result = repair_delivery_manifest(manifest)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    summary = result["summary"]
    print(
        f"applied={summary['applied_count']} proposed={summary['proposed_count']} "
        f"escalated={summary['escalated_count']} rejected={summary['rejected_count']} "
        f"risk={summary['risk_before']}->{summary['risk_after']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
