#!/usr/bin/env python3
"""Pre-transcription hard-song router with strict held-song evaluation."""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Sequence

import librosa
import numpy as np
from eval.bootstrap import song_bootstrap_ci
from eval.canonical import read_json, write_json
from eval.metrics import normalize_text


def _audio_features(stem_path: Path, mix_path: Path) -> dict[str, float]:
    stem, sample_rate = librosa.load(str(stem_path), sr=16000, mono=True, duration=60.0)
    mix, _ = librosa.load(str(mix_path), sr=16000, mono=True, duration=60.0)
    length = min(len(stem), len(mix))
    stem, mix = stem[:length], mix[:length]
    hop = 320
    rms_stem = librosa.feature.rms(y=stem, frame_length=1024, hop_length=hop)[0]
    rms_mix = librosa.feature.rms(y=mix, frame_length=1024, hop_length=hop)[0]
    active_threshold = max(1e-5, float(np.percentile(rms_stem, 80)) * 0.12) if len(rms_stem) else 1e-5
    vocal_activity = float(np.mean(rms_stem >= active_threshold)) if len(rms_stem) else 0.0
    onset = librosa.onset.onset_strength(y=stem, sr=sample_rate, hop_length=hop)
    peaks = librosa.util.peak_pick(onset, pre_max=2, post_max=2, pre_avg=4, post_avg=4, delta=.20, wait=3)
    duration = length / sample_rate
    tempo, _beats = librosa.beat.beat_track(y=mix, sr=sample_rate, hop_length=hop)
    tempo_value = float(np.asarray(tempo).reshape(-1)[0]) if np.size(tempo) else 0.0
    try:
        pitch, voiced, _probability = librosa.pyin(
            stem, fmin=librosa.note_to_hz("C2"), fmax=librosa.note_to_hz("C7"),
            sr=sample_rate, frame_length=2048, hop_length=hop,
        )
        voiced = np.asarray(voiced, dtype=bool)
        pitch = np.asarray(pitch, dtype=float)
        valid = pitch[voiced & np.isfinite(pitch)]
        if len(valid) >= 2:
            semitone = 12.0 * np.log2(valid / max(1e-6, float(np.median(valid))))
            articulation = int(np.sum(np.abs(np.diff(semitone)) >= 0.75))
        else:
            articulation = 0
        voiced_fraction = float(np.mean(voiced)) if len(voiced) else 0.0
    except Exception:
        articulation, voiced_fraction = 0, 0.0
    stem_rms = float(np.sqrt(np.mean(stem * stem))) if len(stem) else 0.0
    mix_rms = float(np.sqrt(np.mean(mix * mix))) if len(mix) else 0.0
    return {
        "audio_probe_duration_s": duration,
        "vocal_activity_fraction": vocal_activity,
        "vocal_onsets_per_second": len(peaks) / max(1e-6, duration),
        "pitch_articulations_per_second": articulation / max(1e-6, duration),
        "voiced_fraction": voiced_fraction,
        "tempo_bpm": tempo_value,
        "vocal_to_mix_rms_ratio": stem_rms / max(1e-6, mix_rms),
        "mix_spectral_flatness": float(np.mean(librosa.feature.spectral_flatness(y=mix))) if len(mix) else 0.0,
    }


def _probe_hypothesis(model, audio_path: Path, language: str | None) -> dict[str, Any]:
    result = model.transcribe(
        str(audio_path), language=language, word_timestamps=False, verbose=None,
        condition_on_previous_text=False, beam_size=1, temperature=0.0, fp16=False,
    )
    segments = result.get("segments") or []
    text = normalize_text(" ".join(str(row.get("text") or "") for row in segments))
    tokens = text.split()
    counts = {token: tokens.count(token) for token in set(tokens)}
    return {
        "text": text,
        "avg_logprob": float(np.mean([float(row.get("avg_logprob") or 0) for row in segments])) if segments else -5.0,
        "max_compression_ratio": max([float(row.get("compression_ratio") or 0) for row in segments] or [0.0]),
        "mean_no_speech_prob": float(np.mean([float(row.get("no_speech_prob") or 0) for row in segments])) if segments else 1.0,
        "words_per_second": len(tokens) / 30.0,
        "unique_token_ratio": len(counts) / max(1, len(tokens)),
        "dominant_token_ratio": max(counts.values()) / max(1, len(tokens)) if counts else 0.0,
    }


def _asr_probe_features(model, stem_path: Path, mix_path: Path, language: str | None) -> dict[str, float]:
    stem, mix = _probe_hypothesis(model, stem_path, language), _probe_hypothesis(model, mix_path, language)
    similarity = SequenceMatcher(None, stem.pop("text"), mix.pop("text"), autojunk=False).ratio()
    return {
        **{f"probe_stem_{key}": float(value) for key, value in stem.items()},
        **{f"probe_mix_{key}": float(value) for key, value in mix.items()},
        "probe_stem_mix_text_similarity": similarity,
        "probe_stem_mix_disagreement": 1.0 - similarity,
    }


def _lid_features(row: dict[str, Any] | None) -> dict[str, float]:
    if not row:
        return {
            "lid_missing": 1.0, "lid_mixed": 0.0, "lid_ambiguous_fraction": 1.0,
            "lid_language_switches": 0.0, "lid_min_confidence": 0.0,
            "lid_mix_fallback": 1.0, "lid_code_switch_candidate": 0.0,
        }
    chunks = row.get("chunks") or []
    languages = [chunk.get("confirmed_language") for chunk in chunks if chunk.get("confirmed_language")]
    switches = sum(left != right for left, right in zip(languages, languages[1:]))
    confidences = [float(chunk.get("detected_confidence") or 0.0) for chunk in chunks]
    return {
        "lid_missing": 0.0,
        "lid_mixed": float(bool(row.get("is_es_en_code_switch"))),
        "lid_ambiguous_fraction": float(row.get("ambiguous_chunk_fraction") or 0.0),
        "lid_language_switches": float(switches),
        "lid_min_confidence": min(confidences) if confidences else 0.0,
        "lid_mix_fallback": float(row.get("input_source") != "full_vocal_stem"),
        "lid_code_switch_candidate": float(bool(
            row.get("acoustic_code_switch_candidate") or row.get("mix_code_switch_candidate")
        )),
    }


def _forced_heavy(row: dict[str, Any]) -> bool:
    lid_switch_uncertain = bool(
        (row.get("lid_mixed") or row.get("lid_code_switch_candidate"))
        and (row.get("lid_mix_fallback") or row.get("lid_ambiguous_fraction", 0) >= .40)
    )
    return bool(row.get("is_live") or row.get("lid_missing") or row.get("lid_mixed") or lid_switch_uncertain)


def build_features(
    golden: Path, cohort_path: Path, lid_path: Path, stem_root: Path, clip_root: Path,
    cache_root: Path | None = None, probe_model=None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cohort = read_json(cohort_path)
    lid_report = read_json(lid_path) if lid_path.is_file() else {"cases": []}
    lid = {row["song_id"]: row for row in lid_report.get("cases") or []}
    rows, failures = [], []
    for label in cohort["cases"]:
        if label["difficult_gold"] is None:
            continue
        song_id = label["song_id"]
        stem, clip = stem_root / song_id / "vocals.wav", clip_root / f"{song_id}.wav"
        if not stem.is_file() or not clip.is_file():
            failures.append({"song_id": song_id, "reason": "missing_30_60s_audio_probe"})
            continue
        cache = cache_root / f"{song_id}.json" if cache_root else None
        signature = {
            "stem_size": stem.stat().st_size, "stem_mtime_ns": stem.stat().st_mtime_ns,
            "mix_size": clip.stat().st_size, "mix_mtime_ns": clip.stat().st_mtime_ns,
        }
        if cache and cache.is_file() and read_json(cache).get("signature") == signature:
            audio_features = read_json(cache)["features"]
        else:
            audio_features = _audio_features(stem, clip)
            if cache:
                write_json(cache, {"schema_version": 1, "signature": signature, "features": audio_features})
        probe_features = {}
        if probe_model is not None:
            probe_cache = cache_root.parent / "asr_probe_cache" / f"{song_id}.json" if cache_root else None
            if probe_cache and probe_cache.is_file() and read_json(probe_cache).get("signature") == signature:
                probe_features = read_json(probe_cache)["features"]
            else:
                meta = read_json(golden / song_id / "meta.json")
                language = (meta.get("language") or {}).get("value") or None
                probe_features = _asr_probe_features(probe_model, stem, clip, language)
                if probe_cache:
                    write_json(probe_cache, {"schema_version": 1, "signature": signature, "features": probe_features})
        rows.append({
            "song_id": song_id,
            "label_difficult": int(label["difficult_gold"]),
            "label_wer": float(label["wer_main"]),
            "is_live": int(label["is_live"]),
            "duration_s": float(label["duration_s"]),
            **audio_features,
            **probe_features,
            **_lid_features(lid.get(song_id)),
        })
    return rows, {"failures": failures, "lid_songs": len(lid)}


def choose_threshold(rows: Sequence[dict[str, Any]], minimum_recall: float = 0.95) -> dict[str, Any]:
    candidates = []
    for threshold in sorted({float(row["oof_probability"]) for row in rows}, reverse=True):
        routed = [
            row for row in rows
            if _forced_heavy(row) or row["oof_probability"] >= threshold
        ]
        positives = sum(row["label_difficult"] for row in rows)
        found = sum(row["label_difficult"] for row in routed)
        recall = found / max(1, positives)
        if recall >= minimum_recall:
            candidates.append((len(routed), -threshold, threshold, recall, routed))
    if not candidates:
        return {"status": "NO_THRESHOLD", "minimum_recall": minimum_recall}
    _count, _negative, threshold, recall, routed = min(candidates, key=lambda row: (row[0], row[1]))
    return {
        "status": "AVAILABLE", "threshold": threshold,
        "difficult_recall": recall,
        "routed_songs": len(routed),
        "routed_fraction": len(routed) / max(1, len(rows)),
        "precision": sum(row["label_difficult"] for row in routed) / max(1, len(routed)),
        "song_ids": [row["song_id"] for row in routed],
    }


def train(rows: list[dict[str, Any]], diagnostics: dict[str, Any], output: Path) -> dict[str, Any]:
    from lightgbm import LGBMClassifier
    from sklearn.metrics import average_precision_score, roc_auc_score
    from sklearn.model_selection import LeaveOneGroupOut

    excluded = {"song_id", "label_difficult", "label_wer"}
    features = [key for key in rows[0] if key not in excluded]
    x = np.asarray([[float(row[key]) for key in features] for row in rows], dtype=np.float32)
    y = np.asarray([row["label_difficult"] for row in rows], dtype=np.int8)
    groups = np.asarray([row["song_id"] for row in rows])
    probabilities = np.zeros(len(rows), dtype=np.float32)
    for train_idx, test_idx in LeaveOneGroupOut().split(x, y, groups):
        model = LGBMClassifier(
            n_estimators=180, learning_rate=.025, max_depth=3, num_leaves=10,
            min_child_samples=8, reg_lambda=4.0, class_weight="balanced",
            random_state=20260830, verbosity=-1,
        )
        model.fit(x[train_idx], y[train_idx])
        probabilities[test_idx] = model.booster_.predict(x[test_idx])
    for row, probability in zip(rows, probabilities):
        row["oof_probability"] = float(probability)
    operating = choose_threshold(rows)
    future_routed_ids = set(operating.get("song_ids") or [])
    # The model probability is held-song, and the operating threshold must be
    # held-song too. Otherwise tuning the threshold on all labels leaks the
    # evaluation song even though the classifier itself is out-of-fold.
    routed_ids = set()
    threshold_failures = []
    for index, row in enumerate(rows):
        calibration = choose_threshold([candidate for offset, candidate in enumerate(rows) if offset != index])
        if calibration.get("status") != "AVAILABLE":
            threshold_failures.append(row["song_id"])
            continue
        forced = _forced_heavy(row)
        if forced or row["oof_probability"] >= float(calibration["threshold"]):
            routed_ids.add(row["song_id"])
    blocks = [{"label": row["label_difficult"], "routed": row["song_id"] in routed_ids} for row in rows]

    def recall(sample: Sequence[dict[str, Any]]) -> float:
        return sum(row["label"] and row["routed"] for row in sample) / max(1, sum(row["label"] for row in sample))

    report = {
        "schema_version": 1,
        "mode": "strict_leave_one_song_out_pretranscription_router",
        "labels": "gold WER/live/reliable session target only; excluded from features",
        "songs": len(rows),
        "features": features,
        "roc_auc": float(roc_auc_score(y, probabilities)),
        "average_precision": float(average_precision_score(y, probabilities)),
        "future_operating_point": operating,
        "held_song_operating_point": {
            "difficult_recall": recall(blocks),
            "routed_songs": len(routed_ids),
            "routed_fraction": len(routed_ids) / max(1, len(rows)),
            "precision": sum(row["label_difficult"] for row in rows if row["song_id"] in routed_ids) / max(1, len(routed_ids)),
            "threshold_calibration_failures": threshold_failures,
        },
        "difficult_recall_song_bootstrap_ci": song_bootstrap_ci(blocks, recall),
        "diagnostics": diagnostics,
        "gate": {
            "requirements": "complete 41-song probes and difficult recall >=95%; uncertain/live route heavy",
            "status": "GO_REPLAY_HEAVY" if len(rows) == 41 and recall(blocks) >= .95 and not threshold_failures else "BLOCKED_INCOMPLETE_COHORT" if len(rows) < 41 else "NO_GO",
        },
    }
    output.mkdir(parents=True, exist_ok=True)
    with (output / "routes.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) + ["route_heavy", "future_route_heavy"])
        writer.writeheader()
        for row in rows:
            writer.writerow({
                **row, "route_heavy": int(row["song_id"] in routed_ids),
                "future_route_heavy": int(row["song_id"] in future_routed_ids),
            })
    write_json(output / "report.json", report)
    return report


def run(
    golden: Path, cohort: Path, lid: Path, stems: Path, clips: Path, output: Path,
    probe_model_name: str | None,
) -> dict[str, Any]:
    probe_model = None
    if probe_model_name:
        import whisper
        probe_model = whisper.load_model(probe_model_name)
    rows, diagnostics = build_features(
        golden, cohort, lid, stems, clips, output / "feature_cache", probe_model,
    )
    diagnostics["asr_probe_model"] = probe_model_name
    feature_table = output / "feature_table.json"
    write_json(feature_table, {"schema_version": 1, "rows": rows, "diagnostics": diagnostics})
    # Torch/Whisper and LightGBM may load conflicting native OpenMP runtimes on
    # macOS. Train in a clean process after feature extraction is persisted.
    command = [
        sys.executable, "-m", "eval.difficulty_router", "--train-only",
        "--features-file", str(feature_table), "--output", str(output),
    ]
    completed = subprocess.run(command, check=True, capture_output=True, text=True)
    return json.loads(completed.stdout)


def train_from_file(features_file: Path, output: Path) -> dict[str, Any]:
    payload = read_json(features_file)
    return train(payload["rows"], payload.get("diagnostics") or {}, output)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", type=Path, default=Path("eval/golden"))
    parser.add_argument("--cohort", type=Path, default=Path("eval/runs/difficult_cohort/report.json"))
    parser.add_argument("--lid", type=Path, default=Path("eval/runs/code_switch_lid/report.json"))
    parser.add_argument("--stems", type=Path, default=Path("eval/cache/language_id/stems"))
    parser.add_argument("--clips", type=Path, default=Path("eval/cache/language_id/clips"))
    parser.add_argument("--output", type=Path, default=Path("eval/runs/difficulty_router"))
    parser.add_argument("--probe-model", default="base")
    parser.add_argument("--disable-asr-probe", action="store_true")
    parser.add_argument("--train-only", action="store_true")
    parser.add_argument("--features-file", type=Path, default=Path("eval/runs/difficulty_router/feature_table.json"))
    args = parser.parse_args()
    if args.train_only:
        result = train_from_file(args.features_file.resolve(), args.output.resolve())
    else:
        result = run(
            *(getattr(args, key).resolve() for key in ("golden", "cohort", "lid", "stems", "clips", "output")),
            None if args.disable_asr_probe else args.probe_model,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
