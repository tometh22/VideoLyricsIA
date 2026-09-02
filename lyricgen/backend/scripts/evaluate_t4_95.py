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
import random
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

    # Report the same song-level uncertainty used by LoRA evaluation.  A
    # global mean can be dominated by one long live song; resampling songs
    # keeps the T4 gate honest and makes the report comparable across cohorts.
    by_song: dict[str, list[tuple[float, float]]] = {}
    for row, before, after in zip(targets, target_before, target_after):
        by_song.setdefault(str(row.get("song_id") or "unknown"), []).append((before, after))
    song_values = {
        song_id: (
            statistics.fmean(before for before, _ in values),
            statistics.fmean(after for _, after in values),
        )
        for song_id, values in by_song.items()
    }
    rng = random.Random(20260901)
    sampled_improvements: list[float] = []
    song_ids = sorted(song_values)
    for _ in range(max(100, 2000)):
        sample = [song_values[rng.choice(song_ids)] for _ in song_ids]
        before = statistics.fmean(value[0] for value in sample)
        after = statistics.fmean(value[1] for value in sample)
        sampled_improvements.append((before - after) / before if before else 0.0)
    sampled_improvements.sort()
    ci_low = sampled_improvements[round(0.025 * (len(sampled_improvements) - 1))]
    ci_high = sampled_improvements[round(0.975 * (len(sampled_improvements) - 1))]
    return {
        "schema_version": 1,
        "replay_only": True,
        "target_population": {"events": len(targets), "mean_error_before_ms": before_mean,
                              "mean_error_after_ms": after_mean,
                              "relative_improvement": improvement,
                              "proposal_rate": len(targets) / len(rows) if rows else 0.0,
                              "song_bootstrap_ci": {
                                  "estimate": statistics.fmean(
                                      (before - after) / before if before else 0.0
                                      for before, after in song_values.values()
                                  ) if song_values else 0.0,
                                  "ci_low": ci_low if song_values else 0.0,
                                  "ci_high": ci_high if song_values else 0.0,
                                  "songs": len(song_values), "iterations": len(sampled_improvements),
                              },
                              "by_song": {
                                  song_id: {"mean_error_before_ms": before, "mean_error_after_ms": after,
                                            "relative_improvement": (before - after) / before if before else 0.0}
                                  for song_id, (before, after) in song_values.items()
                              }},
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
