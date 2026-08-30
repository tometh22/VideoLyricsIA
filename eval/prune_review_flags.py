#!/usr/bin/env python3
"""Cost-calibrated, song-held-out review queue for text and timing flags.

The two risk models use only pre-human line features.  Historical edits define
the replay labels, never the features.  Timing lines approved by the separate
timing-confidence selector leave the human queue; unsafe approvals count as
misses in replay rather than being silently credited.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from lightgbm import LGBMClassifier
from sklearn.metrics import average_precision_score, roc_auc_score
from sklearn.model_selection import LeaveOneGroupOut

from eval.bootstrap import song_bootstrap_ci
from eval.canonical import read_json, write_json
from eval.train_error_predictor import build_dataset


FALSE_FLAG_COST_S = 10.0
MISSED_CORRECTION_COST_S = 60.0
TEXT_CATEGORIES = {"text_only", "text_and_timing", "deleted_line"}
TIMING_CATEGORIES = {"timing_only", "text_and_timing"}


def _gold_maps(ztlr: dict[str, Any]) -> tuple[dict[tuple[str, int], dict[str, Any]], dict[str, int]]:
    rows, added = {}, {}
    for song in ztlr["by_song"]:
        song_id = str(song["song_id"])
        added[song_id] = 0
        for unit in song["units"]:
            if unit["category"] == "added_line":
                added[song_id] += 1
            if unit.get("raw_idx") is not None:
                rows[(song_id, int(unit["raw_idx"]))] = unit
    return rows, added


def _selector_rows(path: Path) -> tuple[dict[tuple[str, int], dict[str, Any]], dict[str, Any]]:
    report_path = path / "report.json"
    csv_path = path / "oof_lines.csv"
    if not report_path.is_file() or not csv_path.is_file():
        return {}, {"status": "MISSING"}
    report = read_json(report_path)
    threshold = float((report.get("operating_points") or {}).get("0.9", {}).get("threshold") or 2.0)
    rows = {}
    with csv_path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            rows[(row["song_id"], int(row["line_idx"]))] = {
                "approved": int(row["hierarchical_abstained"]) == 0 and float(row["oof_probability"]) >= threshold,
                "safe": int(row["label_safe"]) == 1,
                "probability": float(row["oof_probability"]),
            }
    return rows, {
        "status": (report.get("cohort_gate") or {}).get("status", "UNKNOWN"),
        "threshold": threshold,
        "songs": report.get("songs"),
    }


def _oof_probabilities(rows: list[dict[str, Any]], features: list[str], target: str) -> np.ndarray:
    x = np.asarray([[float(row[key]) for key in features] for row in rows], dtype=np.float32)
    y = np.asarray([int(row[target]) for row in rows], dtype=np.int8)
    groups = np.asarray([row["song_id"] for row in rows])
    probabilities = np.zeros(len(rows), dtype=np.float32)
    for train_idx, test_idx in LeaveOneGroupOut().split(x, y, groups):
        model = LGBMClassifier(
            n_estimators=300, learning_rate=0.025, max_depth=4, num_leaves=18,
            min_child_samples=20, reg_lambda=3.0, class_weight="balanced",
            random_state=20260830, verbosity=-1,
        )
        model.fit(x[train_idx], y[train_idx])
        probabilities[test_idx] = model.booster_.predict(x[test_idx])
    return probabilities


def build_rows(golden: Path, ztlr_path: Path, selector_path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    ztlr = read_json(ztlr_path)
    units, added = _gold_maps(ztlr)
    selector, selector_meta = _selector_rows(selector_path)
    raw_rows = build_dataset(golden)
    excluded = {"song_id", "line_idx", "text", "label_corrected", "correction_events"}
    features = [key for key in raw_rows[0] if key not in excluded]
    rows = []
    for raw in raw_rows:
        key = (str(raw["song_id"]), int(raw["line_idx"]))
        unit = units.get(key, {"category": "zero_touch", "approved_idx": None})
        category = str(unit["category"])
        approved_key = (
            (key[0], int(unit["approved_idx"]))
            if unit.get("approved_idx") is not None else None
        )
        timing_selector = selector.get(approved_key, {}) if approved_key else {}
        rows.append({
            **raw,
            "category": category,
            "label_text": int(category in TEXT_CATEGORIES),
            "label_timing": int(category in TIMING_CATEGORIES),
            "label_any": int(category != "zero_touch"),
            "timing_selector_approved": int(bool(timing_selector.get("approved"))),
            "timing_selector_safe": int(bool(timing_selector.get("safe"))),
        })
    text_probability = _oof_probabilities(rows, features, "label_text")
    timing_probability = _oof_probabilities(rows, features, "label_timing")
    for row, text_score, timing_score in zip(rows, text_probability, timing_probability):
        row["text_oof_probability"] = float(text_score)
        row["timing_oof_probability"] = float(timing_score)
    return rows, {
        "features": features,
        "added_lines_by_song": added,
        "timing_selector": selector_meta,
    }


def _evaluate(
    rows: Sequence[dict[str, Any]], added: dict[str, int], text_threshold: float, timing_threshold: float,
) -> dict[str, Any]:
    selected, auto_safe, auto_unsafe = [], [], []
    for row in rows:
        text_flag = row["text_oof_probability"] >= text_threshold
        timing_flag = row["timing_oof_probability"] >= timing_threshold
        auto = bool(row["timing_selector_approved"])
        human = bool(text_flag or (timing_flag and not auto))
        if human:
            selected.append(row)
        if auto and row["timing_selector_safe"] and row["label_timing"]:
            auto_safe.append(row)
        if auto and not row["timing_selector_safe"]:
            auto_unsafe.append(row)
    corrections = [row for row in rows if row["label_any"]]
    found_keys = {(row["song_id"], row["line_idx"]) for row in selected + auto_safe}
    found = [row for row in corrections if (row["song_id"], row["line_idx"]) in found_keys]
    total_added = sum(added.values())
    total_corrections = len(corrections) + total_added
    false_flags = [row for row in selected if not row["label_any"]]
    missed = total_corrections - len(found)
    selected_keys = {(item["song_id"], item["line_idx"]) for item in selected}
    auto_safe_keys = {(item["song_id"], item["line_idx"]) for item in auto_safe}
    expected_cost = 0.0
    for row in rows:
        combined_probability = max(float(row["text_oof_probability"]), float(row["timing_oof_probability"]))
        key = (row["song_id"], row["line_idx"])
        if key in selected_keys:
            expected_cost += FALSE_FLAG_COST_S * (1.0 - combined_probability)
        elif key not in auto_safe_keys:
            expected_cost += MISSED_CORRECTION_COST_S * combined_probability
    expected_cost += total_added * MISSED_CORRECTION_COST_S
    category_lines = {
        "text_real": sum(row["category"] in TEXT_CATEGORIES for row in selected),
        "timing_residual": sum(row["category"] == "timing_only" for row in selected),
        "false": len(false_flags),
    }
    return {
        "text_threshold": text_threshold,
        "timing_threshold": timing_threshold,
        "selected_lines": len(selected),
        "false_flags": len(false_flags),
        "corrections_total_including_added_lines": total_corrections,
        "corrections_found_or_safely_auto_resolved": len(found),
        "correction_recall": len(found) / max(1, total_corrections),
        "auto_resolved_safe_timing_lines": len(auto_safe),
        "unsafe_timing_selector_approvals": len(auto_unsafe),
        "unaddressable_added_lines": total_added,
        "missed_corrections": missed,
        "queue_seconds": len(selected) * FALSE_FLAG_COST_S,
        "queue_seconds_per_song": len(selected) * FALSE_FLAG_COST_S / max(1, len(added)),
        "realized_search_and_queue_cost_seconds": len(selected) * FALSE_FLAG_COST_S + missed * MISSED_CORRECTION_COST_S,
        "realized_search_and_queue_cost_seconds_per_song": (
            len(selected) * FALSE_FLAG_COST_S + missed * MISSED_CORRECTION_COST_S
        ) / max(1, len(added)),
        "expected_cost_seconds": expected_cost,
        "expected_cost_seconds_per_song": expected_cost / max(1, len(added)),
        "seconds_decomposition_per_song": {
            key: value * FALSE_FLAG_COST_S / max(1, len(added)) for key, value in category_lines.items()
        },
    }


def _grid(rows: list[dict[str, Any]], added: dict[str, int]) -> list[dict[str, Any]]:
    text_values = np.unique(np.r_[
        0.0, np.quantile([row["text_oof_probability"] for row in rows], np.linspace(0, 1, 101)), 1.000001,
    ])
    timing_values = np.unique(np.r_[
        0.0, np.quantile([row["timing_oof_probability"] for row in rows], np.linspace(0, 1, 101)), 1.000001,
    ])
    return [_evaluate(rows, added, float(text), float(timing)) for text in text_values for timing in timing_values]


def _choose(points: Sequence[dict[str, Any]], recall: float | None) -> dict[str, Any]:
    eligible = list(points) if recall is None else [row for row in points if row["correction_recall"] >= recall]
    if not eligible:
        return {"status": "NO_POINT", "required_recall": recall}
    selected = min(eligible, key=lambda row: (row["expected_cost_seconds"], row["selected_lines"], row["false_flags"]))
    return {"status": "AVAILABLE", "required_recall": recall, **selected}


def run(golden: Path, ztlr: Path, selector: Path, output: Path) -> dict[str, Any]:
    rows, metadata = build_rows(golden, ztlr, selector)
    points = _grid(rows, metadata["added_lines_by_song"])
    operating = {
        "minimum_expected_cost": _choose(points, None),
        "recall_90": _choose(points, 0.90),
        "recall_93": _choose(points, 0.93),
        "recall_95": _choose(points, 0.95),
    }
    chosen = operating["recall_93"]
    text_y = np.asarray([row["label_text"] for row in rows])
    timing_y = np.asarray([row["label_timing"] for row in rows])
    report = {
        "schema_version": 1,
        "mode": "cost_calibrated_strict_leave_one_song_out_review_queue",
        "costs": {"false_flag_seconds": FALSE_FLAG_COST_S, "missed_correction_seconds": MISSED_CORRECTION_COST_S},
        "songs": len(metadata["added_lines_by_song"]),
        "lines": len(rows),
        "validation": "LeaveOneGroupOut separately for text and timing risk",
        "models": {
            "text": {
                "roc_auc": float(roc_auc_score(text_y, [row["text_oof_probability"] for row in rows])),
                "average_precision": float(average_precision_score(text_y, [row["text_oof_probability"] for row in rows])),
            },
            "timing": {
                "roc_auc": float(roc_auc_score(timing_y, [row["timing_oof_probability"] for row in rows])),
                "average_precision": float(average_precision_score(timing_y, [row["timing_oof_probability"] for row in rows])),
            },
        },
        "timing_selector_input": metadata["timing_selector"],
        "operating_points": operating,
        "gate": {
            "requirements": {"correction_recall": 0.93, "maximum_false_flags": 120},
            "status": "GO_REPLAY" if chosen.get("correction_recall", 0.0) >= 0.93 and chosen.get("false_flags", 10**9) <= 120 else "NO_GO",
        },
    }
    if metadata["timing_selector"].get("status") != "COMPLETE":
        report["gate"]["status"] = "BLOCKED_INCOMPLETE_TIMING_SELECTOR"
    songs = sorted(metadata["added_lines_by_song"])
    chosen_text = float(chosen.get("text_threshold") or 2.0)
    chosen_timing = float(chosen.get("timing_threshold") or 2.0)
    blocks = [
        {"song_id": song, "rows": [row for row in rows if row["song_id"] == song],
         "added": metadata["added_lines_by_song"].get(song, 0)}
        for song in songs
    ]

    def bootstrap_recall(sample: Sequence[dict[str, Any]]) -> float:
        flat = [row for block in sample for row in block["rows"]]
        sample_added = {str(index): int(block["added"]) for index, block in enumerate(sample)}
        stats = _evaluate(flat, sample_added, chosen_text, chosen_timing)
        return float(stats["correction_recall"])

    report["chosen_recall_song_bootstrap_ci"] = song_bootstrap_ci(blocks, bootstrap_recall)
    output.mkdir(parents=True, exist_ok=True)
    with (output / "oof_lines.csv").open("w", newline="", encoding="utf-8") as handle:
        fields = list(rows[0].keys())
        writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader(); writer.writerows(rows)
    write_json(output / "report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", type=Path, default=Path("eval/golden"))
    parser.add_argument("--ztlr", type=Path, default=Path("eval/runs/ztlr_baseline/report.json"))
    parser.add_argument("--selector", type=Path, default=Path("eval/runs/timing_confidence"))
    parser.add_argument("--output", type=Path, default=Path("eval/runs/pruned_review_flags"))
    args = parser.parse_args()
    report = run(args.golden.resolve(), args.ztlr.resolve(), args.selector.resolve(), args.output.resolve())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
