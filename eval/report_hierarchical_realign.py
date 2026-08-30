"""Publish the occurrence-anchor replay without overstating its ZTLR effect."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

from eval.bootstrap import percentile
from eval.canonical import read_json, write_json


def _variant(report: dict[str, Any], aligner: str) -> list[dict[str, Any]]:
    return report["aligners"][aligner]["loo_display_calibration"]["variants"]["robust_global_median"]["by_song"]


def _descriptive(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    lines = [line for song in rows for line in song["per_line"]]
    boundaries = [abs(float(line[key])) for line in lines for key in ("start_error_ms", "end_error_ms")]
    return {
        "songs": len(rows), "aligned_lines": len(lines), "boundaries": len(boundaries),
        "p50_boundary_abs_ms": percentile(boundaries, 0.50) if boundaries else None,
        "p90_boundary_abs_ms": percentile(boundaries, 0.90) if boundaries else None,
        "boundaries_over_1s": sum(value > 1000 for value in boundaries),
        "boundaries_over_2s": sum(value > 2000 for value in boundaries),
    }


def build(
    baseline_path: Path, hierarchical_path: Path, ztlr_path: Path, output: Path,
) -> dict[str, Any]:
    baseline_report, hierarchical_report = read_json(baseline_path), read_json(hierarchical_path)
    ztlr = read_json(ztlr_path)
    baseline = _variant(baseline_report, "current_xlsr")
    hierarchical = _variant(hierarchical_report, "current_xlsr_hierarchical")
    common = sorted({row["song_id"] for row in baseline} & {row["song_id"] for row in hierarchical})
    baseline_common = [row for row in baseline if row["song_id"] in common]
    hierarchical_common = [row for row in hierarchical if row["song_id"] in common]
    baseline_stats, hierarchical_stats = _descriptive(baseline_common), _descriptive(hierarchical_common)

    units = {
        song["song_id"]: {
            int(unit["approved_idx"]): unit
            for unit in song["units"] if unit.get("approved_idx") is not None
        }
        for song in ztlr["by_song"]
    }
    timing_only_available = timing_only_resolved = 0
    for song in hierarchical:
        for line in song["per_line"]:
            unit = units[song["song_id"]].get(int(line["line_idx"]))
            if not unit or unit["category"] != "timing_only":
                continue
            timing_only_available += 1
            if abs(float(line["start_error_ms"])) <= 150 and abs(float(line["end_error_ms"])) <= 150:
                timing_only_resolved += 1
    combined_zero_touch = int(ztlr["zero_touch_lines"]) + timing_only_resolved
    theoretical_zero_touch = int(ztlr["zero_touch_lines"]) + int(ztlr["category_counts"]["timing_only"])
    report = {
        "schema_version": 1,
        "experiment": "hierarchical-final-text-realignment-with-occurrence-anchors",
        "approved_timing_supplied_to_aligner": False,
        "common_cohort": {
            "song_ids": common,
            "baseline": baseline_stats,
            "hierarchical": hierarchical_stats,
            "relative_p90_improvement": (
                (baseline_stats["p90_boundary_abs_ms"] - hierarchical_stats["p90_boundary_abs_ms"])
                / baseline_stats["p90_boundary_abs_ms"]
            ),
            "over_2s_reduction": baseline_stats["boundaries_over_2s"] - hierarchical_stats["boundaries_over_2s"],
        },
        "available_stem_cohort": hierarchical_report["aligners"]["current_xlsr_hierarchical"]["loo_display_calibration"]["variants"]["robust_global_median"]["metrics"],
        "coverage": {
            "eligible_songs": hierarchical_report["eligible_songs"],
            "audio_available": hierarchical_report["audio_available"],
            "songs_scored": len(hierarchical),
        },
        "ztlr": {
            "historical": ztlr["ztlr"],
            "theoretical_if_all_timing_only_were_solved": theoretical_zero_touch / int(ztlr["work_units"]),
            "timing_only_lines_in_scored_songs": timing_only_available,
            "timing_only_lines_reproduced_within_150ms": timing_only_resolved,
            "measured_combined_lower_bound": combined_zero_touch / int(ztlr["work_units"]),
            "warning": "the 86% figure is a ceiling, not a measured outcome",
        },
        "decision": {
            "global_realign": "NO_GO",
            "review_suggestions": "CONTINUE_CALIBRATION",
            "reason": "occurrence tail improved materially, but p90 remains above 250 ms and coverage is selective",
        },
    }
    write_json(output, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=Path("eval/runs/final_text_realign/report.json"))
    parser.add_argument("--hierarchical", type=Path, default=Path("eval/runs/final_text_realign_hierarchical_26/report.json"))
    parser.add_argument("--ztlr", type=Path, default=Path("eval/runs/ztlr_baseline/report.json"))
    parser.add_argument("--output", type=Path, default=Path("eval/runs/hierarchical_realign_report/report.json"))
    args = parser.parse_args()
    result = build(args.baseline.resolve(), args.hierarchical.resolve(), args.ztlr.resolve(), args.output.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
