"""Calibrate a surgical review queue from out-of-fold flag sources.

Every score consumed here must be out-of-fold by song.  The optimizer targets
95% recall of genuinely corrected lines, then minimizes the number of lines a
reviewer must inspect.  Audio time is measured as the union of selected raw
line windows with bounded context, never as line-count times a guessed cost.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from eval.bootstrap import song_bootstrap_ci
from eval.canonical import read_json, segments_to_lines, write_json


def _csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _merge_duration(intervals: Sequence[tuple[float, float]]) -> float:
    if not intervals:
        return 0.0
    total = 0.0
    start, end = sorted(intervals)[0]
    for next_start, next_end in sorted(intervals)[1:]:
        if next_start <= end:
            end = max(end, next_end)
        else:
            total += end - start
            start, end = next_start, next_end
    return total + end - start


def _load_rows(golden: Path, predictor: Path, timing: Path) -> list[dict[str, Any]]:
    predictor_rows = _csv(predictor)
    timing_scores = {
        (row["song_id"], int(row["line_idx"])): float(row["oof_probability"])
        for row in _csv(timing)
    }
    manifest = read_json(golden / "manifest.json")
    raw_by_song: dict[str, list[dict[str, Any]]] = {}
    duration_by_song: dict[str, float] = {}
    for item in manifest["cases"]:
        if item["raw_quality"] not in {"exact", "reconstructed"}:
            continue
        case = golden / item["path"]
        raw_by_song[item["song_id"]] = segments_to_lines(read_json(case / "raw_pipeline_output.json")["segments"])
        duration_by_song[item["song_id"]] = float(read_json(case / "meta.json")["duration_s"])
    rows = []
    for row in predictor_rows:
        song_id, line_idx = row["song_id"], int(row["line_idx"])
        lines = raw_by_song.get(song_id, [])
        if not 0 <= line_idx < len(lines):
            continue
        line = lines[line_idx]
        duration = duration_by_song[song_id]
        rows.append({
            "song_id": song_id, "line_idx": line_idx,
            "label": int(row["label_corrected"]),
            "correction_events": int(row.get("correction_events") or 0),
            "predictor": float(row["probability"]),
            "timing": timing_scores.get((song_id, line_idx), 0.0),
            "start_s": max(0.0, float(line["start_s"]) - 1.0),
            "end_s": min(duration, float(line["end_s"]) + 1.0),
            "song_duration_s": duration,
        })
    return rows


def _selected(row: dict[str, Any], predictor_threshold: float, timing_threshold: float) -> bool:
    return row["predictor"] >= predictor_threshold or row["timing"] >= timing_threshold


def _stats(rows: Sequence[dict[str, Any]], predictor_threshold: float, timing_threshold: float) -> dict[str, float]:
    selected = [row for row in rows if _selected(row, predictor_threshold, timing_threshold)]
    positives = sum(row["label"] for row in rows)
    found = sum(row["label"] for row in selected)
    events = sum(row["correction_events"] for row in rows)
    events_found = sum(row["correction_events"] for row in selected)
    by_song: dict[str, list[tuple[float, float]]] = defaultdict(list)
    durations: dict[str, float] = {}
    for row in rows:
        durations[row["song_id"]] = row["song_duration_s"]
    for row in selected:
        by_song[row["song_id"]].append((row["start_s"], row["end_s"]))
    flagged_seconds = sum(_merge_duration(intervals) for intervals in by_song.values())
    audio_seconds = sum(durations.values())
    return {
        "corrected_line_recall": found / max(1, positives),
        "correction_event_recall": events_found / max(1, events),
        "selected_lines": float(len(selected)),
        "false_flags": float(sum(not row["label"] for row in selected)),
        "precision": found / max(1, len(selected)),
        "flagged_audio_seconds": flagged_seconds,
        "total_audio_seconds": audio_seconds,
        "flagged_audio_fraction": flagged_seconds / max(1.0, audio_seconds),
    }


def calibrate(golden: Path, predictor: Path, timing: Path, output: Path) -> dict[str, Any]:
    rows = _load_rows(golden, predictor, timing)
    candidates = []
    grid = np.linspace(0.0, 1.0, 101)
    for predictor_threshold in grid:
        for timing_threshold in grid:
            stats = _stats(rows, float(predictor_threshold), float(timing_threshold))
            if stats["corrected_line_recall"] >= 0.95:
                candidates.append((
                    stats["selected_lines"], stats["flagged_audio_seconds"],
                    -stats["precision"], float(predictor_threshold), float(timing_threshold), stats,
                ))
    if not candidates:
        raise RuntimeError("no threshold pair reaches 95% corrected-line recall")
    _, _, _, predictor_threshold, timing_threshold, stats = min(candidates)
    songs = sorted({row["song_id"] for row in rows})
    song_blocks = [[row for row in rows if row["song_id"] == song] for song in songs]

    def bootstrap_stat(name: str):
        if name == "flagged_audio_seconds":
            return song_bootstrap_ci(
                song_blocks,
                lambda blocks: sum(_stats(block, predictor_threshold, timing_threshold)[name] for block in blocks),
            )
        if name == "flagged_audio_fraction":
            return song_bootstrap_ci(
                song_blocks,
                lambda blocks: (
                    sum(_stats(block, predictor_threshold, timing_threshold)["flagged_audio_seconds"] for block in blocks)
                    / max(1.0, sum(_stats(block, predictor_threshold, timing_threshold)["total_audio_seconds"] for block in blocks))
                ),
            )
        return song_bootstrap_ci(
            song_blocks,
            lambda blocks: _stats(
                [row for block in blocks for row in block], predictor_threshold, timing_threshold,
            )[name],
        )

    selected_rows = [
        {**row, "selected": _selected(row, predictor_threshold, timing_threshold)}
        for row in rows
    ]
    by_song = []
    for song in songs:
        song_rows = [row for row in rows if row["song_id"] == song]
        song_stats = _stats(song_rows, predictor_threshold, timing_threshold)
        by_song.append({"song_id": song, **song_stats})
    report = {
        "schema_version": 1,
        "mode": "out_of_fold_song_level_flag_union",
        "sources": {
            "general_error_predictor": str(predictor),
            "timing_detector": str(timing),
            "t7": "NOT_YET_TRAINED",
            "auto_consistency": "NOT_YET_AVAILABLE_FOR_41",
            "vad_coverage": "NOT_YET_JOINED",
        },
        "songs": len(songs), "lines": len(rows),
        "thresholds": {"general_error_predictor": predictor_threshold, "timing_detector": timing_threshold},
        "metrics": stats,
        "confidence_intervals": {
            name: bootstrap_stat(name)
            for name in ("corrected_line_recall", "correction_event_recall", "precision", "false_flags", "flagged_audio_seconds", "flagged_audio_fraction")
        },
        "reviewer_projection": {
            "seconds_flagged_per_song": stats["flagged_audio_seconds"] / max(1, len(songs)),
            "full_audio_seconds_per_song": stats["total_audio_seconds"] / max(1, len(songs)),
            "interpretation": "search-time projection only; editing minutes require the instrumented reviewer timer",
        },
        "by_song": by_song,
        "selected_rows": selected_rows,
    }
    write_json(output, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", type=Path, default=Path("eval/golden"))
    parser.add_argument("--predictor", type=Path, default=Path("eval/runs/error_predictor_v2/review_queue_oof.csv"))
    parser.add_argument("--timing", type=Path, default=Path("eval/runs/t4_learned_v2/classification_dataset.csv"))
    parser.add_argument("--output", type=Path, default=Path("eval/runs/flag_union/report.json"))
    args = parser.parse_args()
    report = calibrate(args.golden.resolve(), args.predictor.resolve(), args.timing.resolve(), args.output.resolve())
    print(json.dumps({
        "songs": report["songs"], "lines": report["lines"],
        "thresholds": report["thresholds"], "metrics": report["metrics"],
        "reviewer_projection": report["reviewer_projection"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
