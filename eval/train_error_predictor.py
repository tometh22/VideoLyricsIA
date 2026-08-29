#!/usr/bin/env python3
"""Train a song-held-out predictor of human correction probability."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

import joblib
import numpy as np
from lightgbm import LGBMClassifier
from sklearn.metrics import average_precision_score, roc_auc_score
from sklearn.model_selection import GroupKFold

from eval.bootstrap import song_bootstrap_ci
from eval.canonical import read_json


def _number(value: Any) -> float:
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def build_dataset(golden: Path) -> list[dict[str, Any]]:
    manifest = read_json(golden / "manifest.json")
    rows = []
    for item in manifest["cases"]:
        case = golden / item["path"]
        meta = read_json(case / "meta.json")
        if meta["raw_quality"] not in {"exact", "reconstructed"}:
            continue
        raw = read_json(case / "raw_pipeline_output.json")["segments"]
        edits = read_json(case / "edits.json")
        corrected = Counter(
            int(edit["line_idx"]) for edit in edits
            if edit.get("line_idx") is not None and edit.get("op") in {
                "text_edit", "start_edit", "end_edit", "line_deleted", "kind_changed",
            }
        )
        normalized = [" ".join(str(segment.get("text") or "").lower().split()) for segment in raw]
        repeats = Counter(normalized)
        for line_idx, segment in enumerate(raw):
            words = segment.get("words") or []
            scores = [_number(word.get("score")) for word in words if word.get("score") is not None]
            text_value = str(segment.get("text") or "")
            provenance = segment.get("content_provenance") or {}
            source = str(segment.get("content_source") or provenance.get("source") or "")
            start = _number(segment.get("start"))
            end = _number(segment.get("end"))
            first_word_start = _number(words[0].get("start")) if words else start
            last_word_end = _number(words[-1].get("end")) if words else end
            previous_end = _number(raw[line_idx - 1].get("end")) if line_idx else start
            next_start = _number(raw[line_idx + 1].get("start")) if line_idx + 1 < len(raw) else end
            word_durations = [
                max(0.0, _number(word.get("end")) - _number(word.get("start")))
                for word in words
            ]
            language = str((meta.get("language") or {}).get("value") or "unknown")
            row = {
                "song_id": item["song_id"], "line_idx": line_idx,
                "text": text_value, "label_corrected": int(corrected[line_idx] > 0),
                "correction_events": corrected[line_idx],
                "duration_s": max(0.0, end - start),
                "word_count": len(text_value.split()), "char_count": len(text_value),
                "line_position": line_idx / max(1, len(raw) - 1),
                "repeated_line": int(repeats[normalized[line_idx]] > 1),
                "repetition_group": int(segment.get("repetition_group") is not None),
                "language_es": int(language == "es"),
                "language_en": int(language == "en"),
                "language_other": int(language not in {"es", "en"}),
                "asr_word_score_mean": float(np.mean(scores)) if scores else 0.0,
                "asr_word_score_min": float(np.min(scores)) if scores else 0.0,
                "asr_word_score_available": int(bool(scores)),
                "word_duration_mean": float(np.mean(word_durations)) if word_durations else 0.0,
                "word_duration_max": float(np.max(word_durations)) if word_durations else 0.0,
                "line_start_padding": first_word_start - start,
                "line_end_padding": end - last_word_end,
                "gap_from_previous": start - previous_end,
                "gap_to_next": next_start - end,
                "boundary_touches_next": int(abs(next_start - end) <= 0.02),
                "fixed_end_padding_250ms": int(abs((end - last_word_end) - 0.25) <= 0.03),
                "ctc_lr": _number(segment.get("ctc_lr")),
                "alignment_score": _number(segment.get("alignment_score")),
                "ctc_mean_score": _number(segment.get("ctc_mean_score")),
                "ctc_min_score": _number(segment.get("ctc_min_score")),
                "recognition_score": _number(segment.get("recognition_score")),
                "family_agreement_word_vote": int(bool(segment.get("word_voted"))),
                "pipeline_review_flag": int(bool(segment.get("review"))),
                "locked": int(bool(segment.get("locked"))),
                "catalog_reference": int("catalog" in source),
                "gap_recovered": int(bool(segment.get("gap_recovered") or segment.get("gap_rescued"))),
                "repetition_recovered": int(bool(segment.get("repetition_recovered"))),
                "interjection_shape": int(text_value.strip(" ()").lower() in {"oh", "ah", "eh", "uh", "yeah"}),
            }
            rows.append(row)
    return rows


def train(rows: list[dict[str, Any]], output: Path) -> dict[str, Any]:
    excluded = {"song_id", "line_idx", "text", "label_corrected", "correction_events"}
    features = [key for key in rows[0] if key not in excluded]
    x = np.asarray([[float(row[key]) for key in features] for row in rows], dtype=np.float32)
    y = np.asarray([row["label_corrected"] for row in rows], dtype=np.int8)
    groups = np.asarray([row["song_id"] for row in rows])
    prediction = np.zeros(len(rows), dtype=np.float32)
    models = []
    for train_index, test_index in GroupKFold(n_splits=5).split(x, y, groups):
        model = LGBMClassifier(
            n_estimators=300, learning_rate=0.03, num_leaves=20,
            min_child_samples=20, reg_lambda=2.0, class_weight="balanced",
            random_state=20260829, verbosity=-1,
        )
        model.fit(x[train_index], y[train_index])
        prediction[test_index] = model.predict_proba(x[test_index])[:, 1]
        models.append(model)
    auc = float(roc_auc_score(y, prediction))
    pr_auc = float(average_precision_score(y, prediction))
    blocks = []
    for song_id in sorted(set(groups)):
        indices = np.where(groups == song_id)[0]
        blocks.append({"song_id": song_id, "labels": y[indices].tolist(), "predictions": prediction[indices].tolist()})

    def bootstrap_auc(sample: list[dict[str, Any]]) -> float:
        labels = [value for block in sample for value in block["labels"]]
        predictions = [value for block in sample for value in block["predictions"]]
        return float(roc_auc_score(labels, predictions)) if len(set(labels)) > 1 else 0.5

    auc_ci = song_bootstrap_ci(blocks, bootstrap_auc)

    def bootstrap_pr_auc(sample: list[dict[str, Any]]) -> float:
        labels = [value for block in sample for value in block["labels"]]
        predictions = [value for block in sample for value in block["predictions"]]
        return float(average_precision_score(labels, predictions)) if any(labels) else 0.0

    def top_third_recall(labels: np.ndarray, scores: np.ndarray) -> float:
        count = max(1, int(np.ceil(len(labels) / 3)))
        selected = np.argsort(-scores)[:count]
        return float(np.sum(labels[selected]) / max(1, np.sum(labels)))

    top_third = top_third_recall(y, prediction)
    current_order_scores = -np.asarray([
        float(row["line_position"]) for row in rows
    ], dtype=np.float32)
    current_order_top_third = top_third_recall(y, current_order_scores)

    def bootstrap_top_third(sample: list[dict[str, Any]]) -> float:
        labels = np.asarray([value for block in sample for value in block["labels"]], dtype=np.int8)
        predictions = np.asarray([value for block in sample for value in block["predictions"]])
        return top_third_recall(labels, predictions)

    report = {
        "schema_version": 2, "model": "lightgbm_error_predictor_v2",
        "rows": len(rows), "songs": len(set(groups)),
        "positive_rows": int(np.sum(y)), "positive_rate": float(np.mean(y)),
        "features": features, "validation": "5-fold GroupKFold by song",
        "auc_song_bootstrap_ci": auc_ci,
        "pr_auc": pr_auc,
        "pr_auc_song_bootstrap_ci": song_bootstrap_ci(blocks, bootstrap_pr_auc),
        "review_queue_efficiency": {
            "fraction_reviewed": 1 / 3,
            "real_corrections_found": top_third,
            "song_bootstrap_ci": song_bootstrap_ci(blocks, bootstrap_top_third),
            "current_line_order_corrections_found": current_order_top_third,
            "random_expected": 1 / 3,
            "interpretation": "ranking only; it does not edit or approve content",
        },
        "independent_family_features": {
            "whisper_word_vote_rows": int(sum(row["family_agreement_word_vote"] for row in rows)),
            "gemini_agreement_rows": 0,
            "auto_consistency_rows": 0,
            "note": "Missing families are reported, not imputed as agreement.",
        },
        "taxonomy_feature": {
            "status": "EXCLUDED_GOLD_LEAKAGE",
            "reason": "the current taxonomy is computed from pre-human versus approved word edits; feeding it to the same predictor would expose the target",
            "safe_future_path": "cross-fit a raw-only taxonomy-risk model inside each outer song fold",
        },
        "repeat_context_denominator": {
            "unit": "reference word",
            "repeated_line_error_rate": 0.06045406546990496,
            "unique_line_error_rate": 0.0552689756816507,
        },
        "gate": {"required_auc": 0.80, "status": "GO_REVIEW_RANKING" if auc >= 0.80 else "NO_GO"},
    }
    output.mkdir(parents=True, exist_ok=True)
    with (output / "review_queue_oof.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "song_id", "line_idx", "probability", "label_corrected", "correction_events", "text",
        ])
        writer.writeheader()
        for index in np.argsort(-prediction):
            row = rows[int(index)]
            writer.writerow({
                "song_id": row["song_id"], "line_idx": row["line_idx"],
                "probability": float(prediction[index]), "label_corrected": row["label_corrected"],
                "correction_events": row["correction_events"], "text": row["text"],
            })
    from eval.canonical import write_json
    write_json(output / "report.json", report)
    if report["gate"]["status"] == "GO_REVIEW_RANKING":
        joblib.dump({"models": models, "features": features}, output / "model.joblib")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", type=Path, default=Path("eval/golden"))
    parser.add_argument("--output", type=Path, default=Path("eval/runs/error_predictor"))
    args = parser.parse_args()
    report = train(build_dataset(args.golden.resolve()), args.output.resolve())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
