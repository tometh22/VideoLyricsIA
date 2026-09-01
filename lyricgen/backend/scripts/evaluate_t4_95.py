#!/usr/bin/env python3
"""Evaluate T4 only on the contracted early-final population + controls.

Input JSONL rows must contain ``event_id``, ``population`` (``target`` or
``control``), ``baseline_error_ms``, ``proposed_error_ms`` and optionally
``reference_end_s``/``proposed_end_s``.  The command is replay-only: it never
writes a Job or changes a timestamp.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
from typing import Any


def evaluate(rows: list[dict[str, Any]], *, min_improvement: float = 0.20,
             max_control_damage_ms: float = 150.0) -> dict[str, Any]:
    targets = [row for row in rows if str(row.get("population") or "").lower() == "target"]
    controls = [row for row in rows if str(row.get("population") or "").lower() == "control"]
    if not targets:
        raise ValueError("T4 replay has no target events")
    def val(row: dict, key: str) -> float:
        try:
            return abs(float(row[key]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"row {row.get('event_id')} missing numeric {key}") from exc
    target_before = [val(row, "baseline_error_ms") for row in targets]
    target_after = [val(row, "proposed_error_ms") for row in targets]
    control_damage = [val(row, "proposed_error_ms") - val(row, "baseline_error_ms") for row in controls]
    before_mean = statistics.fmean(target_before) if target_before else 0.0
    after_mean = statistics.fmean(target_after) if target_after else 0.0
    improvement = (before_mean - after_mean) / before_mean if before_mean else 0.0
    damaged = [row for row, delta in zip(controls, control_damage) if delta > max_control_damage_ms]
    return {
        "schema_version": 1,
        "replay_only": True,
        "target_population": {"events": len(targets), "mean_error_before_ms": before_mean,
                              "mean_error_after_ms": after_mean,
                              "relative_improvement": improvement,
                              "proposal_rate": len(targets) / len(rows) if rows else 0.0},
        "control_population": {"events": len(controls),
                               "damaged_over_threshold": len(damaged),
                               "max_damage_threshold_ms": max_control_damage_ms,
                               "damage_event_ids": [str(row.get("event_id")) for row in damaged]},
        "gate": {
            "minimum_relative_improvement": min_improvement,
            "no_control_damage": not damaged,
            "passed": bool(improvement >= min_improvement and not damaged),
            "mutation_allowed": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--min-improvement", type=float, default=0.20)
    parser.add_argument("--max-control-damage-ms", type=float, default=150.0)
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.input.read_text(encoding="utf-8").splitlines() if line.strip()]
    report = evaluate(rows, min_improvement=args.min_improvement,
                      max_control_damage_ms=args.max_control_damage_ms)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["gate"]["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
