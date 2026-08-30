"""Gold-stratified replay of reviewer work after hierarchical realignment.

This is an evaluation decomposition, not a production classifier: the gold is
used only to say which historical flags were text, timing or false alarms.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from eval.canonical import read_json, write_json


def _merged_seconds(intervals: Iterable[tuple[float, float]]) -> float:
    merged: list[list[float]] = []
    for start, end in sorted(intervals):
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return sum(end - start for start, end in merged)


def build(flags_path: Path, ztlr_path: Path, hierarchical_path: Path, output: Path) -> dict[str, Any]:
    flags = [row for row in read_json(flags_path)["selected_rows"] if row.get("selected")]
    ztlr = read_json(ztlr_path)
    hierarchical_report = read_json(hierarchical_path)
    aligned = hierarchical_report["aligners"]["current_xlsr_hierarchical"]["loo_display_calibration"]["variants"]["robust_global_median"]["by_song"]
    timing_success = {
        (song["song_id"], int(line["line_idx"]))
        for song in aligned for line in song["per_line"]
        if abs(float(line["start_error_ms"])) <= 150 and abs(float(line["end_error_ms"])) <= 150
    }
    approved_by_raw = {
        (song["song_id"], int(unit["raw_idx"])): unit
        for song in ztlr["by_song"] for unit in song["units"]
        if unit.get("raw_idx") is not None
    }
    scored_songs = {song["song_id"] for song in aligned}
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for flag in flags:
        song_id, raw_idx = str(flag["song_id"]), int(flag["line_idx"])
        if song_id not in scored_songs:
            continue
        unit = approved_by_raw.get((song_id, raw_idx))
        category = str((unit or {}).get("category") or "unmapped")
        resolved = bool(
            category == "timing_only"
            and unit.get("approved_idx") is not None
            and (song_id, int(unit["approved_idx"])) in timing_success
        )
        bucket = "timing_resolved" if resolved else (
            "text_review" if category in {"text_only", "text_and_timing", "deleted_line"}
            else "timing_review" if category == "timing_only"
            else "false_or_zero_touch" if category == "zero_touch"
            else "unmapped"
        )
        buckets[bucket].append(flag)

    def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
        by_song: dict[str, list[tuple[float, float]]] = defaultdict(list)
        for row in rows:
            by_song[str(row["song_id"])].append((float(row["start_s"]), float(row["end_s"])))
        seconds = sum(_merged_seconds(values) for values in by_song.values())
        return {
            "lines": len(rows), "songs": len(by_song), "audio_seconds": seconds,
            "seconds_per_scored_song": seconds / max(1, len(scored_songs)),
        }

    report = {
        "schema_version": 1,
        "mode": "post_realign_review_gold_stratification",
        "warning": "uses approved history only for retrospective decomposition; not deployable as a selector",
        "scored_songs": len(scored_songs),
        "selected_flag_lines": sum(len(rows) for rows in buckets.values()),
        "buckets": {name: summary(rows) for name, rows in sorted(buckets.items())},
        "review_unit": "line",
        "decision": "timing-resolved lines may leave the queue only after a production-safe selector is calibrated",
    }
    write_json(output, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--flags", type=Path, default=Path("eval/runs/flag_union/report.json"))
    parser.add_argument("--ztlr", type=Path, default=Path("eval/runs/ztlr_baseline/report.json"))
    parser.add_argument("--hierarchical", type=Path, default=Path("eval/runs/final_text_realign_hierarchical_26/report.json"))
    parser.add_argument("--output", type=Path, default=Path("eval/runs/post_realign_review/report.json"))
    args = parser.parse_args()
    result = build(args.flags.resolve(), args.ztlr.resolve(), args.hierarchical.resolve(), args.output.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
