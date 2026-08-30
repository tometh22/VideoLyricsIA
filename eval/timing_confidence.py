#!/usr/bin/env python3
"""Song-held-out confidence selector for final-text timing realignment.

This module never changes timestamps.  It estimates whether both display
edges produced by the hierarchical aligner are within 150 ms of the approved
historical result.  Approved timing is used only as the replay label.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import soundfile as sf
from lightgbm import LGBMClassifier
from sklearn.metrics import average_precision_score, roc_auc_score
from sklearn.model_selection import LeaveOneGroupOut

from eval.bootstrap import song_bootstrap_ci
from eval.canonical import read_json, write_json
from eval.metrics import normalize_text


TARGET_EDGE_MS = 150.0
LIVE_PATTERN = re.compile(r"\b(?:live|en vivo)\b", re.IGNORECASE)


def _variant(report: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    name, payload = next(iter(report["aligners"].items()))
    calibrated = payload["loo_display_calibration"]
    return name, calibrated["variants"]["robust_global_median"]["by_song"]


def _display_edges(line: dict[str, Any]) -> tuple[float, float]:
    return (
        float(line["predicted_start"]) + float(line.get("start_display_delta_ms") or 0.0) / 1000.0,
        float(line["predicted_end"]) + float(line.get("end_display_delta_ms") or 0.0) / 1000.0,
    )


def _prediction_files(root: Path) -> dict[str, dict[str, Any]]:
    candidates = [path for path in root.glob("*/*.json") if path.name != "report.json"]
    return {path.stem: read_json(path) for path in candidates}


def _vad_density(path: Path, start_s: float, end_s: float) -> float:
    if not path.is_file() or end_s <= start_s:
        return 0.0
    info = sf.info(str(path))
    left = max(0, int(start_s * info.samplerate))
    frames = max(0, min(info.frames, int(end_s * info.samplerate)) - left)
    if frames <= 0:
        return 0.0
    audio, _ = sf.read(str(path), start=left, frames=frames, dtype="float32", always_2d=True)
    mono = np.mean(audio, axis=1)
    frame = max(1, int(0.04 * info.samplerate))
    hop = max(1, int(0.02 * info.samplerate))
    if len(mono) < frame:
        rms = np.asarray([math.sqrt(float(np.mean(mono * mono))) if len(mono) else 0.0])
    else:
        rms = np.asarray([
            math.sqrt(float(np.mean(mono[index:index + frame] ** 2)))
            for index in range(0, len(mono) - frame + 1, hop)
        ])
    peak = float(np.percentile(rms, 90)) if len(rms) else 0.0
    threshold = max(1e-4, peak * 0.10)
    return float(np.mean(rms >= threshold)) if len(rms) else 0.0


def _metadata(golden: Path) -> dict[str, dict[str, Any]]:
    result = {}
    for item in read_json(golden / "manifest.json")["cases"]:
        if item["raw_quality"] not in {"exact", "reconstructed"}:
            continue
        meta = read_json(golden / item["path"] / "meta.json")
        result[item["song_id"]] = {**meta, "case_path": item["path"]}
    return result


def build_dataset(
    golden: Path, stems: Path, global_report_path: Path,
    hierarchical_report_path: Path, hierarchical_root: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    global_report, hierarchical_report = read_json(global_report_path), read_json(hierarchical_report_path)
    global_name, global_songs = _variant(global_report)
    hierarchical_name, hierarchical_songs = _variant(hierarchical_report)
    global_lines = {
        (song["song_id"], int(line["line_idx"])): line
        for song in global_songs for line in song["per_line"] if line.get("aligned")
    }
    hierarchical_lines = {
        (song["song_id"], int(line["line_idx"])): line
        for song in hierarchical_songs for line in song["per_line"] if line.get("aligned")
    }
    persisted = _prediction_files(hierarchical_root)
    meta_by_song = _metadata(golden)
    rows: list[dict[str, Any]] = []
    excluded_live, missing_global, missing_witness = set(), set(), 0
    for song_id, persisted_song in sorted(persisted.items()):
        meta = meta_by_song.get(song_id)
        if not meta:
            continue
        title = str(meta.get("title") or "")
        is_live = bool(LIVE_PATTERN.search(title))
        if is_live:
            excluded_live.add(song_id)
            continue
        case = golden / meta["case_path"]
        approved = read_json(case / "approved.json")
        normalized = [normalize_text(str(line.get("text") or "")) for line in approved]
        repeats = Counter(normalized)
        repeated_share = sum(repeats[value] > 1 for value in normalized) / max(1, len(normalized))
        song_type = "short" if float(meta["duration_s"]) < 180 else "repetitive" if repeated_share >= 0.35 else "standard"
        prediction = persisted_song.get("prediction") or []
        metadata = persisted_song.get("metadata") or {}
        hard_indices = [int(row["approved_idx"]) for row in metadata.get("hard_anchor_evidence") or []]
        for line_idx in range(len(approved)):
            key = (song_id, line_idx)
            global_line, hierarchical_line = global_lines.get(key), hierarchical_lines.get(key)
            row_prediction = prediction[line_idx] if line_idx < len(prediction) else {}
            witness = row_prediction.get("selector_witness") or {}
            abstained = not hierarchical_line or row_prediction.get("start") is None or row_prediction.get("end") is None
            if global_line is None:
                missing_global.add(song_id)
                continue
            global_start, global_end = _display_edges(global_line)
            if hierarchical_line:
                hier_start, hier_end = _display_edges(hierarchical_line)
                label = int(
                    abs(float(hierarchical_line["start_error_ms"])) <= TARGET_EDGE_MS
                    and abs(float(hierarchical_line["end_error_ms"])) <= TARGET_EDGE_MS
                )
            else:
                hier_start = hier_end = 0.0
                label = 0
            local_start, local_end = witness.get("local_start"), witness.get("local_end")
            if not witness:
                missing_witness += 1
            before = [anchor for anchor in hard_indices if anchor <= line_idx]
            after = [anchor for anchor in hard_indices if anchor >= line_idx]
            selected_start = hier_start if not abstained else global_start
            selected_end = hier_end if not abstained else global_end
            rows.append({
                "song_id": song_id,
                "line_idx": line_idx,
                "song_type": song_type,
                "label_safe": label,
                "hierarchical_abstained": int(abstained),
                "global_hier_start_disagreement_ms": abs(global_start - hier_start) * 1000 if not abstained else 10000.0,
                "global_hier_end_disagreement_ms": abs(global_end - hier_end) * 1000 if not abstained else 10000.0,
                "global_local_start_disagreement_ms": abs(float(witness["global_start"]) - float(local_start)) * 1000 if witness.get("global_start") is not None and local_start is not None else 10000.0,
                "global_local_end_disagreement_ms": abs(float(witness["global_end"]) - float(local_end)) * 1000 if witness.get("global_end") is not None and local_end is not None else 10000.0,
                "hard_anchor_lines_before": int(witness.get("hard_anchor_lines_before", line_idx - max(before) if before else len(approved))),
                "hard_anchor_lines_after": int(witness.get("hard_anchor_lines_after", min(after) - line_idx if after else len(approved))),
                "text_occurrences": int(witness.get("text_occurrences", repeats[normalized[line_idx]])),
                "unique_line": int(repeats[normalized[line_idx]] == 1),
                "local_ctc_score": float(witness.get("local_ctc_score") or 0.0),
                "global_ctc_score": float(witness.get("global_ctc_score") or 0.0),
                "vad_density": _vad_density(stems / song_id / "vocals.wav", selected_start, selected_end),
                "predicted_duration_s": max(0.0, selected_end - selected_start),
                "line_position": line_idx / max(1, len(approved) - 1),
                "word_count": len(normalized[line_idx].split()),
                "character_count": len(normalized[line_idx].replace(" ", "")),
            })
    diagnostics = {
        "global_aligner": global_name,
        "hierarchical_aligner": hierarchical_name,
        "songs_with_global_and_hierarchical": len({row["song_id"] for row in rows}),
        "live_songs_excluded": sorted(excluded_live),
        "songs_missing_global_witness": sorted(missing_global),
        "rows_missing_persisted_selector_witness": missing_witness,
    }
    return rows, diagnostics


def _precision(rows: Sequence[dict[str, Any]], threshold: float) -> float:
    selected = [row for row in rows if not row["hierarchical_abstained"] and row["oof_probability"] >= threshold]
    return sum(row["label_safe"] for row in selected) / max(1, len(selected))


def _operating_point(rows: list[dict[str, Any]], target: float) -> dict[str, Any]:
    eligible = [row for row in rows if not row["hierarchical_abstained"]]
    thresholds = sorted({float(row["oof_probability"]) for row in eligible}, reverse=True)
    options = []
    for threshold in thresholds:
        selected = [row for row in eligible if row["oof_probability"] >= threshold]
        precision = sum(row["label_safe"] for row in selected) / max(1, len(selected))
        if precision >= target:
            options.append((len(selected), -threshold, threshold, precision, selected))
    if not options:
        return {"target_precision": target, "status": "NO_THRESHOLD", "approved_lines": 0, "approval_fraction": 0.0}
    _, _, threshold, precision, selected = max(options)
    songs = sorted({row["song_id"] for row in rows})
    blocks = [[row for row in selected if row["song_id"] == song] for song in songs]

    def block_precision(sample: Sequence[list[dict[str, Any]]]) -> float:
        flat = [row for block in sample for row in block]
        return sum(row["label_safe"] for row in flat) / max(1, len(flat))

    return {
        "target_precision": target,
        "status": "AVAILABLE",
        "threshold": threshold,
        "precision": precision,
        "precision_song_bootstrap_ci": song_bootstrap_ci(blocks, block_precision),
        "approved_lines": len(selected),
        "correct_approved_lines": sum(row["label_safe"] for row in selected),
        "approval_fraction": len(selected) / max(1, len(eligible)),
    }


def train(rows: list[dict[str, Any]], ztlr_path: Path, output: Path) -> dict[str, Any]:
    if len({row["song_id"] for row in rows}) < 5:
        raise RuntimeError("timing selector requires at least five complete non-live songs")
    excluded = {"song_id", "line_idx", "song_type", "label_safe"}
    features = [key for key in rows[0] if key not in excluded]
    x = np.asarray([[float(row[key]) for key in features] for row in rows], dtype=np.float32)
    y = np.asarray([row["label_safe"] for row in rows], dtype=np.int8)
    groups = np.asarray([row["song_id"] for row in rows])
    probabilities = np.zeros(len(rows), dtype=np.float32)
    for train_idx, test_idx in LeaveOneGroupOut().split(x, y, groups):
        model = LGBMClassifier(
            n_estimators=240, learning_rate=0.025, max_depth=4, num_leaves=15,
            min_child_samples=20, reg_lambda=3.0, class_weight="balanced",
            random_state=20260830, verbosity=-1,
        )
        model.fit(x[train_idx], y[train_idx])
        probabilities[test_idx] = model.booster_.predict(x[test_idx])
    for row, probability in zip(rows, probabilities):
        row["oof_probability"] = 0.0 if row["hierarchical_abstained"] else float(probability)

    points = {str(value): _operating_point(rows, value) for value in (0.90, 0.93, 0.95)}
    chosen = points["0.9"]
    threshold = float(chosen.get("threshold") or 2.0)
    selected_keys = {
        (row["song_id"], row["line_idx"])
        for row in rows if not row["hierarchical_abstained"] and row["oof_probability"] >= threshold and row["label_safe"]
    }
    ztlr = read_json(ztlr_path)
    automation_songs = {row["song_id"] for row in rows}
    timing_only = {
        (song["song_id"], int(unit["approved_idx"]))
        for song in ztlr["by_song"] if song["song_id"] in automation_songs
        for unit in song["units"]
        if unit.get("category") == "timing_only" and unit.get("approved_idx") is not None
    }
    resolved = timing_only & selected_keys
    measured_ztlr = (int(ztlr["zero_touch_lines"]) + len(resolved)) / max(1, int(ztlr["work_units"]))
    abstention_by_type = {}
    for song_type in sorted({row["song_type"] for row in rows}):
        subset = [row for row in rows if row["song_type"] == song_type]
        abstention_by_type[song_type] = {
            "songs": len({row["song_id"] for row in subset}),
            "lines": len(subset),
            "hierarchical_abstention_rate": sum(row["hierarchical_abstained"] for row in subset) / max(1, len(subset)),
            "selector_approval_rate_at_90_precision": sum(
                not row["hierarchical_abstained"] and row["oof_probability"] >= threshold for row in subset
            ) / max(1, len(subset)),
        }
    report = {
        "schema_version": 1,
        "mode": "strict_leave_one_song_out_timing_confidence",
        "approved_timing_role": "label_only",
        "songs": len(set(groups)),
        "lines": len(rows),
        "features": features,
        "validation": "LeaveOneGroupOut; one held song per fold",
        "auc": float(roc_auc_score(y, probabilities)),
        "average_precision": float(average_precision_score(y, probabilities)),
        "operating_points": points,
        "gate": {
            "requirement": "precision >= 0.90 on approved lines; hierarchical abstentions never approved",
            "status": "GO_REPLAY_PARTIAL" if chosen.get("precision", 0.0) >= 0.90 else "NO_GO",
        },
        "timing_only": {
            "eligible_non_live_lines": len(timing_only),
            "correctly_auto_resolved_at_90_precision": len(resolved),
        },
        "ztlr": {
            "historical": float(ztlr["ztlr"]),
            "measured_with_correctly_resolved_timing_only": measured_ztlr,
            "note": "denominator remains the full historical 41-song work-unit set; live lines receive no automation",
        },
        "abstention_by_song_type": abstention_by_type,
    }
    output.mkdir(parents=True, exist_ok=True)
    with (output / "oof_lines.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader(); writer.writerows(rows)
    write_json(output / "report.json", report)
    return report


def run(
    golden: Path, stems: Path, global_report: Path, hierarchical_report: Path,
    hierarchical_root: Path, ztlr: Path, output: Path,
) -> dict[str, Any]:
    rows, diagnostics = build_dataset(golden, stems, global_report, hierarchical_report, hierarchical_root)
    report = train(rows, ztlr, output)
    report["inputs"] = diagnostics
    complete = (
        diagnostics["songs_with_global_and_hierarchical"] >= 38
        and not diagnostics["songs_missing_global_witness"]
        and diagnostics["rows_missing_persisted_selector_witness"] == 0
    )
    report["cohort_gate"] = {
        "status": "COMPLETE" if complete else "INCOMPLETE",
        "requirement": "all non-live songs have both witnesses and selector telemetry",
    }
    if not complete:
        report["gate"]["status"] = "BLOCKED_INCOMPLETE_COHORT"
    write_json(output / "report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", type=Path, default=Path("eval/golden"))
    parser.add_argument("--stems", type=Path, default=Path("eval/cache/full_stems"))
    parser.add_argument("--global-report", type=Path, default=Path("eval/runs/final_text_realign/report.json"))
    parser.add_argument("--hierarchical-report", type=Path, default=Path("eval/runs/final_text_realign_hierarchical_26/report.json"))
    parser.add_argument("--hierarchical-root", type=Path, default=Path("eval/runs/final_text_realign_hierarchical_26"))
    parser.add_argument("--ztlr", type=Path, default=Path("eval/runs/ztlr_baseline/report.json"))
    parser.add_argument("--output", type=Path, default=Path("eval/runs/timing_confidence"))
    args = parser.parse_args()
    report = run(*[getattr(args, name).resolve() for name in (
        "golden", "stems", "global_report", "hierarchical_report", "hierarchical_root", "ztlr", "output",
    )])
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
