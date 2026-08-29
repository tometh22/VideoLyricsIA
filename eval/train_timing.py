#!/usr/bin/env python3
"""Train the T4 perceptual end-timing regressor with song-block CV."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

import joblib
import librosa
import numpy as np
from lightgbm import LGBMRegressor
from sklearn.model_selection import GroupKFold

from eval.bootstrap import song_bootstrap_ci
from eval.canonical import read_json, write_json


def _mean(values: np.ndarray) -> float:
    return float(np.mean(values)) if len(values) else 0.0


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


def build_dataset(golden: Path, qualities: set[str]) -> list[dict[str, Any]]:
    manifest = read_json(golden / "manifest.json")
    rows: list[dict[str, Any]] = []
    for item in manifest["cases"]:
        case = golden / item["path"]
        meta = read_json(case / "meta.json")
        if meta["raw_quality"] not in qualities:
            continue
        edits = [
            edit for edit in read_json(case / "edits.json")
            if edit.get("op") == "end_edit"
            and edit.get("line_idx") is not None
            and not bool(edit.get("derived"))
        ]
        if not edits:
            continue
        approved = read_json(case / "approved.json")
        normalized_lines = [" ".join(str(segment.get("text") or "").lower().split()) for segment in approved]
        repeats = Counter(normalized_lines)
        audio_path = case / meta["audio"]["filename"]
        audio, sample_rate = librosa.load(audio_path, sr=16000, mono=True)
        for edit in edits:
            line_idx = int(edit["line_idx"])
            if not 0 <= line_idx < len(approved):
                continue
            try:
                before, after = float(edit["before"]), float(edit["after"])
            except (TypeError, ValueError):
                continue
            delta_ms = 1000.0 * (after - before)
            if abs(delta_ms) < 1.0:
                continue
            segment = approved[line_idx]
            text_value = str(segment.get("text") or "")
            tokens = text_value.split()
            last_word = tokens[-1].strip(".,;:!?…()[]{}\"'") if tokens else ""
            row = {
                "song_id": item["song_id"], "artist": meta.get("artist") or "",
                "line_idx": line_idx, "target_delta_ms": delta_ms,
                "boundary_s": before, "direct_observation": not bool(edit.get("derived")),
                "line_word_count": len(tokens), "line_char_count": len(text_value),
                "last_word_char_count": len(last_word),
                "last_word_ends_vowel": int(last_word[-1:].lower() in "aeiouáéíóúü"),
                "ends_punctuation": int(text_value.rstrip()[-1:] in ".,;:!?…"),
                "repeated_line": int(repeats[normalized_lines[line_idx]] > 1),
                "line_position": line_idx / max(1, len(approved) - 1),
                "approved_line_duration_s": max(0.0, float(segment.get("end", 0)) - float(segment.get("start", 0))),
                **_audio_features(audio, sample_rate, before),
            }
            rows.append(row)
    return rows


def train(rows: list[dict[str, Any]], output: Path) -> dict[str, Any]:
    if len({row["song_id"] for row in rows}) < 5:
        raise RuntimeError("timing CV requires at least five songs")
    excluded = {"song_id", "artist", "line_idx", "target_delta_ms", "boundary_s", "direct_observation"}
    features = [key for key in rows[0] if key not in excluded]
    x = np.asarray([[float(row[key]) for key in features] for row in rows], dtype=np.float32)
    y = np.asarray([float(row["target_delta_ms"]) for row in rows], dtype=np.float32)
    groups = np.asarray([row["song_id"] for row in rows])
    predictions = np.zeros(len(rows), dtype=np.float32)
    uncertainty = np.zeros(len(rows), dtype=np.float32)
    folds = GroupKFold(n_splits=5)
    models = []
    for fold_index, (train_index, test_index) in enumerate(folds.split(x, y, groups)):
        fold_predictions = []
        for seed in (20260829, 20260830, 20260831):
            model = LGBMRegressor(
                n_estimators=300, learning_rate=0.03, num_leaves=20,
                min_child_samples=20, reg_lambda=2.0, random_state=seed,
                objective="huber", verbosity=-1,
            )
            model.fit(x[train_index], y[train_index])
            fold_predictions.append(model.predict(x[test_index]))
            models.append(model)
        fold_matrix = np.asarray(fold_predictions)
        predictions[test_index] = np.mean(fold_matrix, axis=0)
        uncertainty[test_index] = np.std(fold_matrix, axis=0)
    absolute_error = np.abs(predictions - y)
    baseline_error = np.abs(y)
    candidates = np.where(np.abs(predictions) >= 150.0)[0]
    selected = np.asarray([], dtype=int)
    threshold = None
    for quantile in (0.25, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 1.0):
        if not len(candidates):
            break
        limit = float(np.quantile(uncertainty[candidates], quantile))
        subset = candidates[uncertainty[candidates] <= limit]
        if len(subset) and float(np.mean(absolute_error[subset] <= 150.0)) >= 0.60:
            if len(subset) > len(selected):
                selected, threshold = subset, limit
    proposed = set(int(index) for index in selected)
    song_blocks = []
    for song_id in sorted(set(groups)):
        indices = [index for index, row in enumerate(rows) if row["song_id"] == song_id and index in proposed]
        song_blocks.append({
            "song_id": song_id, "proposals": len(indices),
            "accurate": sum(absolute_error[index] <= 150.0 for index in indices),
        })
    accuracy_ci = None
    if selected.size:
        accuracy_ci = song_bootstrap_ci(
            song_blocks,
            lambda sample: sum(row["accurate"] for row in sample) / max(1, sum(row["proposals"] for row in sample)),
        )
    report = {
        "schema_version": 1, "model": "lightgbm_t4_perceptual_v1",
        "validation": "5-fold GroupKFold by song; 3-seed fold ensemble",
        "rows": len(rows), "songs": len(set(groups)), "features": features,
        "oof": {
            "mae_ms": float(np.mean(absolute_error)),
            "baseline_no_change_mae_ms": float(np.mean(baseline_error)),
            "within_150ms_all": float(np.mean(absolute_error <= 150.0)),
        },
        "proposal_gate": {
            "status": "GO_SUGGESTIONS" if selected.size and accuracy_ci and accuracy_ci["estimate"] >= 0.60 else "NO_GO",
            "uncertainty_threshold_ms": threshold,
            "proposals": int(selected.size),
            "coverage_of_observed_end_corrections": float(selected.size / len(rows)),
            "mean_proposals_per_song": float(selected.size / len(set(groups))),
            "within_150ms_song_bootstrap_ci": accuracy_ci,
            "required_point_accuracy": 0.60,
        },
        "per_song": song_blocks,
    }
    output.mkdir(parents=True, exist_ok=True)
    with (output / "dataset.csv").open("w", newline="", encoding="utf-8") as handle:
        fieldnames = list(rows[0]) + ["oof_prediction_delta_ms", "oof_uncertainty_ms", "oof_abs_error_ms", "proposed"]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for index, row in enumerate(rows):
            writer.writerow({
                **row, "oof_prediction_delta_ms": float(predictions[index]),
                "oof_uncertainty_ms": float(uncertainty[index]),
                "oof_abs_error_ms": float(absolute_error[index]), "proposed": index in proposed,
            })
    write_json(output / "report.json", report)
    if report["proposal_gate"]["status"] == "GO_SUGGESTIONS":
        joblib.dump({"models": models, "features": features}, output / "model.joblib")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", type=Path, default=Path("eval/golden"))
    parser.add_argument("--output", type=Path, default=Path("eval/runs/t4_learned"))
    args = parser.parse_args()
    rows = build_dataset(args.golden.resolve(), {"exact", "reconstructed"})
    report = train(rows, args.output.resolve())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
