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
from sklearn.metrics import roc_auc_score
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
            row = {
                "song_id": item["song_id"], "line_idx": line_idx,
                "text": text_value, "label_corrected": int(corrected[line_idx] > 0),
                "correction_events": corrected[line_idx],
                "duration_s": max(0.0, _number(segment.get("end")) - _number(segment.get("start"))),
                "word_count": len(text_value.split()), "char_count": len(text_value),
                "line_position": line_idx / max(1, len(raw) - 1),
                "repeated_line": int(repeats[normalized[line_idx]] > 1),
                "asr_word_score_mean": float(np.mean(scores)) if scores else 0.0,
                "asr_word_score_min": float(np.min(scores)) if scores else 0.0,
                "asr_word_score_available": int(bool(scores)),
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
    blocks = []
    for song_id in sorted(set(groups)):
        indices = np.where(groups == song_id)[0]
        blocks.append({"song_id": song_id, "labels": y[indices].tolist(), "predictions": prediction[indices].tolist()})

    def bootstrap_auc(sample: list[dict[str, Any]]) -> float:
        labels = [value for block in sample for value in block["labels"]]
        predictions = [value for block in sample for value in block["predictions"]]
        return float(roc_auc_score(labels, predictions)) if len(set(labels)) > 1 else 0.5

    auc_ci = song_bootstrap_ci(blocks, bootstrap_auc)
    report = {
        "schema_version": 1, "model": "lightgbm_error_predictor_v1",
        "rows": len(rows), "songs": len(set(groups)),
        "positive_rows": int(np.sum(y)), "positive_rate": float(np.mean(y)),
        "features": features, "validation": "5-fold GroupKFold by song",
        "auc_song_bootstrap_ci": auc_ci,
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
