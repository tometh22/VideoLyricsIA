#!/usr/bin/env python3
"""Evaluate shadow endpoint proposals against interval gold JSONL."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from timing_endpoint_gold import evaluate_experiment


def _jsonl(path: Path) -> list[dict]:
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON at {path}:{number}") from exc
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", required=True, type=Path)
    parser.add_argument("--predictions", required=True, type=Path)
    parser.add_argument("--review-metrics", type=Path)
    parser.add_argument("--threshold-frozen-before-test", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = evaluate_experiment(
        _jsonl(args.gold), _jsonl(args.predictions),
        threshold_frozen_before_test=args.threshold_frozen_before_test,
        review_rows=_jsonl(args.review_metrics) if args.review_metrics else (),
    )
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
