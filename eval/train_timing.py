#!/usr/bin/env python3
"""Diagnose and train the two-stage T4 timing suggestion model."""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import librosa
import numpy as np
from lightgbm import LGBMClassifier, LGBMRegressor
from sklearn.metrics import average_precision_score, precision_recall_curve, roc_auc_score
from sklearn.model_selection import GroupKFold

from eval.bootstrap import percentile, song_bootstrap_ci
from eval.canonical import read_json, write_json
from eval.metrics import normalize_text


def _mean(values: np.ndarray) -> float:
    return float(np.mean(values)) if len(values) else 0.0


def _number(value: Any) -> float:
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _text_identity(left: str, right: str) -> float:
    left_norm, right_norm = normalize_text(left), normalize_text(right)
    if not left_norm or not right_norm:
        return 0.0
    return SequenceMatcher(None, left_norm, right_norm).ratio()


def _collapse_endpoint_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Collapse a mouse-drag history to one auditable net endpoint edit."""

    ordered = sorted(events, key=lambda edit: int(edit.get("seq") or 0))
    if not ordered:
        raise ValueError("at least one endpoint event is required")
    first_before = _number(ordered[0].get("before"))
    last_after = _number(ordered[-1].get("after"))
    continuity_errors = sum(
        abs(_number(left.get("after")) - _number(right.get("before"))) > 0.150
        for left, right in zip(ordered, ordered[1:])
    )
    return {
        "first_before_s": first_before,
        "last_after_s": last_after,
        "target_delta_ms": 1000.0 * (last_after - first_before),
        "event_count": len(ordered),
        "intermediate_drag_events": len(ordered) - 1,
        "continuity_errors": continuity_errors,
    }


def _audio_features(audio: np.ndarray, sample_rate: int, boundary_s: float) -> dict[str, float]:
    def window(left: float, right: float) -> np.ndarray:
        start = max(0, int((boundary_s + left) * sample_rate))
        end = min(len(audio), int((boundary_s + right) * sample_rate))
        return audio[start:end]

    before = window(-0.40, 0.0)
    after_short = window(0.0, 0.40)
    after_long = window(0.0, 1.20)

    def rms(signal: np.ndarray) -> float:
        return float(np.sqrt(np.mean(np.square(signal)))) if len(signal) else 0.0

    before_rms, after_rms, tail_rms = rms(before), rms(after_short), rms(after_long)
    if len(after_long) >= 512:
        zcr = _mean(librosa.feature.zero_crossing_rate(y=after_long, frame_length=512, hop_length=160))
        centroid = _mean(librosa.feature.spectral_centroid(y=after_long, sr=sample_rate, n_fft=512, hop_length=160))
        try:
            pitch = librosa.yin(after_long, fmin=65, fmax=1000, sr=sample_rate, frame_length=1024, hop_length=160)
            voiced_ratio = float(np.mean(np.isfinite(pitch)))
            pitch_median = float(np.nanmedian(pitch)) if np.any(np.isfinite(pitch)) else 0.0
        except Exception:
            voiced_ratio = pitch_median = 0.0
    else:
        zcr = centroid = voiced_ratio = pitch_median = 0.0
    return {
        "rms_before_400ms": before_rms,
        "rms_after_400ms": after_rms,
        "rms_after_1200ms": tail_rms,
        "rms_after_over_before": after_rms / max(before_rms, 1e-6),
        "zcr_after": zcr,
        "spectral_centroid_after": centroid,
        "voiced_ratio_after": voiced_ratio,
        "pitch_median_after": pitch_median,
    }


def _line_features(
    segment: dict[str, Any], line_idx: int, line_count: int,
    repeats: Counter[str], audio: np.ndarray, sample_rate: int, boundary_s: float,
) -> dict[str, float]:
    text_value = str(segment.get("text") or "")
    tokens = text_value.split()
    last_word = tokens[-1].strip(".,;:!?…()[]{}\"'") if tokens else ""
    word_scores = [
        _number(word.get("score")) for word in (segment.get("words") or [])
        if word.get("score") is not None
    ]
    normalized = normalize_text(text_value)
    return {
        "line_word_count": float(len(tokens)),
        "line_char_count": float(len(text_value)),
        "last_word_char_count": float(len(last_word)),
        "last_word_ends_vowel": float(last_word[-1:].lower() in "aeiouáéíóúü"),
        "ends_punctuation": float(text_value.rstrip()[-1:] in ".,;:!?…"),
        "repeated_line": float(repeats[normalized] > 1),
        "line_position": line_idx / max(1, line_count - 1),
        "source_line_duration_s": max(0.0, _number(segment.get("end")) - _number(segment.get("start"))),
        "asr_score_mean": float(np.mean(word_scores)) if word_scores else 0.0,
        "asr_score_min": float(np.min(word_scores)) if word_scores else 0.0,
        "asr_score_available": float(bool(word_scores)),
        "ctc_lr": _number(segment.get("ctc_lr")),
        "alignment_score": _number(segment.get("alignment_score")),
        "recognition_score": _number(segment.get("recognition_score")),
        "locked": float(bool(segment.get("locked"))),
        "review_flag": float(bool(segment.get("review"))),
        **_audio_features(audio, sample_rate, boundary_s),
    }


def build_datasets(golden: Path, qualities: set[str]) -> dict[str, Any]:
    """Collapse drag events to one net correction and remove identity leaks.

    Legacy audit rows record every intermediate drag. They are evidence that a
    boundary was touched, but treating all intermediate values as independent
    targets makes the same line appear many times with contradictory deltas.
    """
    manifest = read_json(golden / "manifest.json")
    classification_rows: list[dict[str, Any]] = []
    regression_rows: list[dict[str, Any]] = []
    audit = Counter()
    per_song_audit = []
    for item in manifest["cases"]:
        case = golden / item["path"]
        meta = read_json(case / "meta.json")
        if meta["raw_quality"] not in qualities:
            continue
        raw = read_json(case / "raw_pipeline_output.json")["segments"]
        approved = read_json(case / "approved.json")
        direct = [edit for edit in read_json(case / "edits.json") if not bool(edit.get("derived"))]
        end_events = [edit for edit in direct if edit.get("op") == "end_edit" and edit.get("line_idx") is not None]
        start_events = [edit for edit in direct if edit.get("op") == "start_edit" and edit.get("line_idx") is not None]
        if not end_events:
            continue
        audit["songs_with_direct_end_edits"] += 1
        audit["direct_end_events"] += len(end_events)
        audit["direct_start_events"] += len(start_events)
        if len(raw) != len(approved):
            audit["songs_with_line_count_change"] += 1
        grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
        grouped_starts: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for edit in end_events:
            grouped[int(edit["line_idx"])].append(edit)
        for edit in start_events:
            grouped_starts[int(edit["line_idx"])].append(edit)
        audit["unique_touched_line_indices"] += len(grouped)
        audit["intermediate_drag_events"] += len(end_events) - len(grouped)
        repeats = Counter(normalize_text(str(segment.get("text") or "")) for segment in raw)
        audio_path = case / meta["audio"]["filename"]
        audio, sample_rate = librosa.load(audio_path, sr=16000, mono=True)
        clean_song_rows = 0
        for line_idx, segment in enumerate(raw):
            if line_idx >= len(approved):
                audit["classification_excluded_missing_approved_index"] += 1
                continue
            identity = _text_identity(str(segment.get("text") or ""), str(approved[line_idx].get("text") or ""))
            if identity < 0.85:
                audit["classification_excluded_identity_mismatch"] += 1
                continue
            events = sorted(grouped.get(line_idx, []), key=lambda edit: int(edit.get("seq") or 0))
            touched = bool(events)
            boundary_s = _number(events[0].get("before")) if touched else _number(segment.get("end"))
            row = {
                "song_id": item["song_id"], "line_idx": line_idx,
                "timing_touched": int(touched), "identity_score": identity,
                "target_delta_ms": 0.0,
                **_line_features(segment, line_idx, len(raw), repeats, audio, sample_rate, boundary_s),
            }
            if touched:
                audit["touched_identity_matched"] += 1
                collapsed = _collapse_endpoint_events(events)
                first_before = collapsed["first_before_s"]
                last_after = collapsed["last_after_s"]
                target_delta_ms = collapsed["target_delta_ms"]
                row["target_delta_ms"] = target_delta_ms
                starts = sorted(grouped_starts.get(line_idx, []), key=lambda edit: int(edit.get("seq") or 0))
                start_delta_ms = (
                    1000.0 * (_number(starts[-1].get("after")) - _number(starts[0].get("before")))
                    if starts else 0.0
                )
                whole_line_relocation = bool(
                    starts and abs(start_delta_ms) > 1000.0
                    and abs(target_delta_ms - start_delta_ms) <= 1000.0
                )
                continuity_errors = collapsed["continuity_errors"]
                final_mismatch_ms = 1000.0 * abs(last_after - _number(approved[line_idx].get("end")))
                if abs(target_delta_ms) > 5000.0:
                    # A five-second net move is a section/line-placement repair,
                    # not the perceptual endpoint adjustment T4 is designed for.
                    audit["regression_excluded_non_endpoint_shift_over_5s"] += 1
                elif whole_line_relocation:
                    audit["regression_excluded_whole_line_relocation"] += 1
                elif continuity_errors:
                    audit["regression_excluded_discontinuous_history"] += 1
                elif final_mismatch_ms > 500.0:
                    audit["regression_excluded_final_snapshot_mismatch"] += 1
                else:
                    regression_rows.append({
                        **row, "event_count": collapsed["event_count"],
                        "first_before_s": first_before, "last_after_s": last_after,
                        "start_delta_ms": start_delta_ms,
                        "final_snapshot_mismatch_ms": final_mismatch_ms,
                    })
                    clean_song_rows += 1
            classification_rows.append(row)
        per_song_audit.append({
            "song_id": item["song_id"], "raw_lines": len(raw), "approved_lines": len(approved),
            "direct_end_events": len(end_events), "unique_touched_indices": len(grouped),
            "clean_regression_rows": clean_song_rows,
        })
    audit["classification_rows"] = len(classification_rows)
    audit["classification_positive_rows"] = sum(row["timing_touched"] for row in classification_rows)
    audit["classification_negative_rows"] = len(classification_rows) - audit["classification_positive_rows"]
    audit["regression_rows"] = len(regression_rows)
    return {
        "classification_rows": classification_rows,
        "regression_rows": regression_rows,
        "audit": dict(audit),
        "per_song_audit": per_song_audit,
        "units": {
            "source_timestamps": "seconds", "target_delta": "milliseconds",
            "target_definition": "last direct observed end after - first direct observed end before, collapsed per song/line",
            "label_definition": "at least one directly observed end_edit for the line index",
        },
    }


def _feature_names(rows: list[dict[str, Any]], extra_excluded: set[str]) -> list[str]:
    excluded = {
        "song_id", "line_idx", "timing_touched", "identity_score", "target_delta_ms",
        "event_count", "first_before_s", "last_after_s", "final_snapshot_mismatch_ms",
        "start_delta_ms",
        *extra_excluded,
    }
    return [key for key in rows[0] if key not in excluded]


def _binary_point(labels: np.ndarray, probabilities: np.ndarray, threshold: float) -> dict[str, float | int]:
    predicted = probabilities >= threshold
    true_positive = int(np.sum(predicted & (labels == 1)))
    return {
        "threshold": threshold, "selected": int(np.sum(predicted)),
        "precision": true_positive / max(1, int(np.sum(predicted))),
        "recall": true_positive / max(1, int(np.sum(labels == 1))),
    }


def train_classifier(rows: list[dict[str, Any]]) -> tuple[dict[str, Any], np.ndarray]:
    if len({row["song_id"] for row in rows}) < 5:
        raise RuntimeError("timing classifier CV requires at least five songs")
    features = _feature_names(rows, set())
    x = np.asarray([[float(row[key]) for key in features] for row in rows], dtype=np.float32)
    y = np.asarray([row["timing_touched"] for row in rows], dtype=np.int8)
    groups = np.asarray([row["song_id"] for row in rows])
    predictions = np.zeros(len(rows), dtype=np.float32)
    for train_index, test_index in GroupKFold(n_splits=5).split(x, y, groups):
        model = LGBMClassifier(
            n_estimators=300, learning_rate=0.03, num_leaves=20,
            min_child_samples=15, reg_lambda=2.0, class_weight="balanced",
            random_state=20260829, verbosity=-1,
        )
        model.fit(x[train_index], y[train_index])
        predictions[test_index] = model.predict_proba(x[test_index])[:, 1]
    auc = float(roc_auc_score(y, predictions))
    pr_auc = float(average_precision_score(y, predictions))
    blocks = []
    for song_id in sorted(set(groups)):
        indices = np.where(groups == song_id)[0]
        blocks.append({"song_id": song_id, "labels": y[indices].tolist(), "scores": predictions[indices].tolist()})

    def metric(name: str):
        def calculate(sample: list[dict[str, Any]]) -> float:
            labels = np.asarray([value for block in sample for value in block["labels"]])
            scores = np.asarray([value for block in sample for value in block["scores"]])
            if len(set(labels.tolist())) < 2:
                return 0.5 if name == "auc" else float(np.mean(labels))
            return float(roc_auc_score(labels, scores) if name == "auc" else average_precision_score(labels, scores))
        return calculate

    precision, recall, thresholds = precision_recall_curve(y, predictions)
    curve = [
        {"threshold": float(thresholds[index]), "precision": float(precision[index]), "recall": float(recall[index])}
        for index in np.linspace(0, max(0, len(thresholds) - 1), min(50, len(thresholds)), dtype=int)
    ] if len(thresholds) else []
    return {
        "rows": len(rows), "songs": len(set(groups)), "positive_rate": float(np.mean(y)),
        "features": features,
        "auc": song_bootstrap_ci(blocks, metric("auc")),
        "pr_auc": song_bootstrap_ci(blocks, metric("pr_auc")),
        "operating_points": [_binary_point(y, predictions, 0.75), _binary_point(y, predictions, 0.80)],
        "precision_recall_curve": curve,
        "validation": "5-fold GroupKFold by song",
    }, predictions


def train_regressor(
    rows: list[dict[str, Any]], classification_rows: list[dict[str, Any]],
) -> tuple[dict[str, Any], np.ndarray]:
    if len({row["song_id"] for row in rows}) < 5:
        raise RuntimeError("timing regressor CV requires at least five songs")
    features = _feature_names(rows, set())
    x = np.asarray([[float(row[key]) for key in features] for row in rows], dtype=np.float32)
    y = np.asarray([float(row["target_delta_ms"]) for row in rows], dtype=np.float32)
    groups = np.asarray([row["song_id"] for row in rows])
    predictions = np.zeros(len(rows), dtype=np.float32)
    median_all_predictions = np.zeros(len(rows), dtype=np.float32)
    median_corrected_predictions = np.zeros(len(rows), dtype=np.float32)
    all_by_song: dict[str, list[float]] = defaultdict(list)
    for row in classification_rows:
        all_by_song[row["song_id"]].append(float(row["target_delta_ms"]) if row["timing_touched"] else 0.0)
    for train_index, test_index in GroupKFold(n_splits=5).split(x, y, groups):
        train_songs = set(groups[train_index])
        fold_all = [value for song_id in train_songs for value in all_by_song[song_id]]
        median_all_predictions[test_index] = float(np.median(fold_all)) if fold_all else 0.0
        median_corrected_predictions[test_index] = float(np.median(y[train_index]))
        model = LGBMRegressor(
            n_estimators=400, learning_rate=0.025, num_leaves=16,
            min_child_samples=12, reg_lambda=3.0, random_state=20260829,
            objective="huber", verbosity=-1,
        )
        model.fit(x[train_index], y[train_index])
        predictions[test_index] = model.predict(x[test_index])

    blocks = []
    for song_id in sorted(set(groups)):
        indices = np.where(groups == song_id)[0]
        blocks.append({
            "song_id": song_id, "target": y[indices].tolist(),
            "model": predictions[indices].tolist(), "zero": np.zeros(len(indices)).tolist(),
            "median_all": median_all_predictions[indices].tolist(),
            "median_corrected": median_corrected_predictions[indices].tolist(),
        })

    def within(name: str):
        def calculate(sample: list[dict[str, Any]]) -> float:
            target = np.asarray([value for block in sample for value in block["target"]])
            prediction = np.asarray([value for block in sample for value in block[name]])
            return float(np.mean(np.abs(prediction - target) <= 150.0))
        return calculate

    def summarize(name: str, values: np.ndarray) -> dict[str, Any]:
        error = np.abs(values - y)
        return {
            "within_150ms_song_bootstrap_ci": song_bootstrap_ci(blocks, within(name)),
            "mae_ms": float(np.mean(error)), "p90_abs_error_ms": percentile(error.tolist(), 0.90),
        }

    results = {
        "zero_delta": summarize("zero", np.zeros(len(y))),
        "training_fold_median_all_lines": summarize("median_all", median_all_predictions),
        "training_fold_median_corrected_only": summarize("median_corrected", median_corrected_predictions),
        "lightgbm": summarize("model", predictions),
    }
    model_estimate = results["lightgbm"]["within_150ms_song_bootstrap_ci"]["estimate"]
    return {
        "rows": len(rows), "songs": len(set(groups)), "features": features,
        "target_distribution_ms": {
            "min": float(np.min(y)), "p50": float(np.median(y)),
            "p90_abs": percentile(np.abs(y).tolist(), 0.90), "max": float(np.max(y)),
            "over_5s": int(np.sum(np.abs(y) > 5000.0)),
        },
        "baselines_and_model": results,
        "gate": {
            "required_within_150ms": 0.60,
            "status": "GO_SUGGESTIONS" if model_estimate >= 0.60 else "NO_GO",
        },
        "validation": "5-fold GroupKFold by song; fold-only medians prevent gold leakage",
    }, predictions


def train(dataset: dict[str, Any], output: Path) -> dict[str, Any]:
    classifier, classifier_oof = train_classifier(dataset["classification_rows"])
    regressor, regressor_oof = train_regressor(dataset["regression_rows"], dataset["classification_rows"])
    report = {
        "schema_version": 2, "model": "t4_two_stage_v2",
        "verdict": regressor["gate"]["status"],
        "dataset_audit": dataset["audit"], "units": dataset["units"],
        "classifier_timing_touched": classifier, "regressor_clean_net_delta": regressor,
        "old_result": {
            "rows": 729, "songs": 23, "within_150ms": 0.037037037037037035,
            "diagnosis": "one row per intermediate drag event; repeated contradictory targets and index drift",
        },
    }
    output.mkdir(parents=True, exist_ok=True)
    with (output / "classification_dataset.csv").open("w", newline="", encoding="utf-8") as handle:
        rows = dataset["classification_rows"]
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]) + ["oof_probability"])
        writer.writeheader()
        for row, probability in zip(rows, classifier_oof):
            writer.writerow({**row, "oof_probability": float(probability)})
    with (output / "regression_dataset.csv").open("w", newline="", encoding="utf-8") as handle:
        rows = dataset["regression_rows"]
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]) + ["oof_prediction_delta_ms"])
        writer.writeheader()
        for row, prediction in zip(rows, regressor_oof):
            writer.writerow({**row, "oof_prediction_delta_ms": float(prediction)})
    write_json(output / "dataset_audit_by_song.json", dataset["per_song_audit"])
    write_json(output / "report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", type=Path, default=Path("eval/golden"))
    parser.add_argument("--output", type=Path, default=Path("eval/runs/t4_learned_v2"))
    args = parser.parse_args()
    dataset = build_datasets(args.golden.resolve(), {"exact", "reconstructed"})
    report = train(dataset, args.output.resolve())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
