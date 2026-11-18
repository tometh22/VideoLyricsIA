"""CPU-only experimental editorial-risk learner, not a correction acceptor.

Only pre-edit segment features enter inference. Labels describe historical
editorial changes, not verified acoustic errors or the correctness of a proposed
replacement. This module has no database, provider, or editor mutation imports.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections import Counter
from pathlib import Path

import numpy as np

SCHEMA = "genly-local-editorial-risk-v1"
SCORE_FIELDS = ("ctc_lr", "ctc_min_score", "ctc_mean_score", "alignment_score",
                "content_asr_score", "recognition_score", "content_asr_min_score")
FEATURES = ("line_count", "word_count", "character_count", "duration", "words_per_second",
            "unique_word_fraction", "punctuation_fraction", "digit_fraction",
            "repeated_adjacent_fraction", "word_timestamp_fraction", "word_score_mean",
            "word_score_missing", *[name for key in SCORE_FIELDS for name in (key, key + "_missing")])
TARGETS = ("any_editorial_edit", "text_or_structure_edit", "timing_edit")
SPLITS = ("train", "calibration", "development_replay")


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def source_features(segments: list[dict]) -> list[float]:
    """Whitelist only observations available before a human edits the line."""
    text = " ".join(str(row.get("text") or "") for row in segments)
    tokens = re.findall(r"\w+", unicodedata.normalize("NFKC", text).casefold())
    intervals = [(a, b) for row in segments
                 if (a := _number(row.get("start"))) is not None
                 and (b := _number(row.get("end"))) is not None and b > a]
    duration = sum(b - a for a, b in intervals)
    words = [word for row in segments for word in (row.get("words") or []) if isinstance(word, dict)]
    scores = [value for word in words if (value := _number(word.get("score"))) is not None]
    timed = sum(_number(word.get("start")) is not None and _number(word.get("end")) is not None
                for word in words)
    result = [len(segments), len(tokens), len(text), duration,
              len(tokens) / max(duration, .1), len(set(tokens)) / max(1, len(tokens)),
              sum(unicodedata.category(c).startswith("P") for c in text) / max(1, len(text)),
              sum(c.isdigit() for c in text) / max(1, len(text)),
              sum(a == b for a, b in zip(tokens, tokens[1:])) / max(1, len(tokens) - 1),
              min(1., timed / max(1, len(tokens))), float(np.mean(scores)) if scores else 0.,
              float(not scores)]
    for key in SCORE_FIELDS:
        values = [value for row in segments if (value := _number(row.get(key))) is not None]
        result.extend([float(np.mean(values)) if values else 0., float(not values)])
    return np.clip(result, -10000., 10000.).tolist()


def labels(row: dict) -> dict[str, bool | None]:
    category = row.get("category")
    allowed = {"unchanged", "text_or_segmentation", "text_style", "timing_with_style",
               "timing_only", "text_or_segmentation_with_timing"}
    if category not in allowed:
        raise ValueError("unknown_editorial_category")
    return {"any_editorial_edit": category != "unchanged",
            "text_or_structure_edit": category in {"text_or_segmentation", "text_or_segmentation_with_timing"},
            "timing_edit": (category in {"timing_only", "timing_with_style", "text_or_segmentation_with_timing"})
            if row.get("timing_comparable") else None}


def validate_partitions(rows: list[dict]) -> None:
    """Reject song, audio, artist, or connected-group leakage across partitions."""
    seen = {key: {} for key in ("job_id", "audio_sha256", "artist", "split_group")}
    ids = set()
    for row in rows:
        if row.get("split") == "excluded":
            continue
        if row.get("split") not in SPLITS:
            raise ValueError("unknown_split")
        if not row.get("before") or not isinstance(row["before"], list):
            raise ValueError("source_segments_required")
        if row.get("example_id") in ids:
            raise ValueError("duplicate_example_id")
        ids.add(row.get("example_id"))
        for key, assignments in seen.items():
            value = str(row.get(key) or "").strip().casefold()
            if not value:
                raise ValueError("missing_partition_identity:" + key)
            if value in assignments and assignments[value] != row["split"]:
                raise ValueError("partition_leakage:" + key)
            assignments[value] = row["split"]


def _sigmoid(values):
    return 1. / (1. + np.exp(-np.clip(values, -35., 35.)))


def _weights(rows):
    counts = Counter(row["job_id"] for row in rows)
    weights = np.array([1. / counts[row["job_id"]] for row in rows])
    return weights / weights.sum()


def _fit(x, y, weights, *, penalty=.1, steps=700):
    beta = np.zeros(x.shape[1])
    prior = float(np.clip(np.dot(weights, y), .001, .999))
    intercept = math.log(prior / (1. - prior))
    for _ in range(steps):
        residual = (_sigmoid(x @ beta + intercept) - y) * weights
        beta -= .08 * (x.T @ residual + penalty * beta)
        intercept -= .08 * float(residual.sum())
    return beta, intercept


def _predict(model, x):
    logits = x @ np.array(model["coefficients"]) + model["intercept"]
    calibration = model["calibration"]
    if calibration["status"] == "fitted_development":
        logits = calibration["slope"] * logits + calibration["intercept"]
    return _sigmoid(logits)


def _metrics(y, probability, rows):
    if not len(y):
        return {"rows": 0}
    weights = _weights(rows)
    p = np.clip(probability, 1e-8, 1 - 1e-8)
    positives, negatives = p[y == 1], p[y == 0]
    # Pairwise AUC is bounded by the size of this local development corpus.
    auc = float(np.mean((positives[:, None] > negatives).astype(float)
                        + .5 * (positives[:, None] == negatives))) if len(positives) and len(negatives) else None
    return {"rows": len(rows), "songs": len({r["job_id"] for r in rows}),
            "groups": len({r["split_group"] for r in rows}), "positives": int(y.sum()),
            "song_macro_brier": float(np.dot(weights, (p - y) ** 2)),
            "row_brier": float(np.mean((p - y) ** 2)),
            "song_macro_log_loss": float(np.dot(weights, -(y * np.log(p) + (1 - y) * np.log(1 - p)))),
            "row_auc": auc}


def train(rows: list[dict], *, corpus_sha256: str) -> tuple[dict, dict]:
    missing_sources = sum(not row.get("before") for row in rows)
    rows = [row for row in rows if row.get("before")]
    validate_partitions(rows)
    partitions = {split: [row for row in rows if row.get("split") == split] for split in SPLITS}
    if len(partitions["train"]) < 20 or len({r["split_group"] for r in partitions["train"]}) < 2:
        raise ValueError("insufficient_training_rows_or_groups")
    matrix = np.array([source_features(row["before"]) for row in partitions["train"]])
    mean, scale = matrix.mean(axis=0), matrix.std(axis=0)
    scale[scale < 1e-6] = 1.
    artifact = {"schema": SCHEMA, "feature_names": list(FEATURES), "mean": mean.tolist(),
                "scale": scale.tolist(), "models": {}, "corpus_sha256": corpus_sha256,
                "capability": "rank_editorial_review_risk", "automatic_apply_allowed": False,
                "evaluation_claim": "development_only", "trained_on_audio": False,
                "coverage": "existing_source_segments_only_missing_lyrics_not_detected",
                "label_semantics": "historical_editorial_change_not_acoustic_error_or_candidate_correctness"}
    report = {"schema": SCHEMA + "-evaluation", "corpus_sha256": corpus_sha256,
              "evaluation_claim": "development_only", "independent_fresh_test": False,
              "excluded_missing_source_segments": missing_sources,
              "targets": {}, "excluded_rows": sum(r.get("split") == "excluded" for r in rows)}
    for target in TARGETS:
        selected = {split: [r for r in values if labels(r)[target] is not None]
                    for split, values in partitions.items()}
        data = {split: (np.array([source_features(r["before"]) for r in values], dtype=float).reshape(-1, len(FEATURES)) - mean) / scale
                for split, values in selected.items()}
        data = {split: np.clip(x, -8., 8.) for split, x in data.items()}
        outcomes = {split: np.array([float(labels(r)[target]) for r in values]) for split, values in selected.items()}
        training = selected["train"]
        y = outcomes["train"]
        if len(y) < 20 or len(set(y)) != 2:
            report["targets"][target] = {"status": "insufficient_label_classes"}
            continue
        weights = _weights(training)
        coefficients, intercept = _fit(data["train"], y, weights)
        model = {"coefficients": coefficients.tolist(), "intercept": intercept,
                 "train_constant": float(np.dot(weights, y)),
                 "calibration": {"status": "uncalibrated_insufficient_groups_or_classes"}}
        calibration, cy = selected["calibration"], outcomes["calibration"]
        if (len(cy) >= 50 and len({r["split_group"] for r in calibration}) >= 3
                and cy.sum() >= 10 and (1 - cy).sum() >= 10):
            logits = (data["calibration"] @ coefficients + intercept).reshape(-1, 1)
            slope, offset = _fit(logits, cy, _weights(calibration), penalty=.02)
            model["calibration"] = {"status": "fitted_development", "method": "platt",
                                    "slope": float(slope[0]), "intercept": offset}
        artifact["models"][target] = model
        target_report = {"status": "trained_experimental", "calibration": model["calibration"]["status"], "partitions": {}}
        for split, values in selected.items():
            probs = _predict(model, data[split])
            metrics = _metrics(outcomes[split], probs, values)
            baseline = _metrics(outcomes[split], np.full(len(values), model["train_constant"]), values)
            target_report["partitions"][split] = {"model": metrics, "train_constant_baseline": baseline}
            if len(cy):
                constant = float(np.dot(_weights(calibration), cy))
                target_report["partitions"][split]["calibration_constant_baseline"] = _metrics(
                    outcomes[split], np.full(len(values), constant), values)
        report["targets"][target] = target_report
    if "any_editorial_edit" not in artifact["models"]:
        raise ValueError("cannot_fit_editorial_risk")
    artifact["model_id"] = hashlib.sha256(json.dumps(artifact, sort_keys=True).encode()).hexdigest()[:20]
    report["model_id"] = artifact["model_id"]
    return artifact, report


def load_artifact(path: str | Path, *, expected_sha256: str | None = None) -> dict:
    """Validate local artifact integrity; this is not runtime release attestation."""
    raw = Path(path).read_bytes()
    if expected_sha256 and hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("artifact_file_hash_mismatch")
    artifact = json.loads(raw)
    if artifact.get("schema") != SCHEMA or artifact.get("feature_names") != list(FEATURES):
        raise ValueError("unsupported_risk_artifact")
    unsigned = {key: value for key, value in artifact.items() if key != "model_id"}
    identity = hashlib.sha256(json.dumps(unsigned, sort_keys=True).encode()).hexdigest()[:20]
    if identity != artifact.get("model_id"):
        raise ValueError("artifact_model_hash_mismatch")
    if artifact.get("automatic_apply_allowed") is not False or artifact.get("capability") != "rank_editorial_review_risk":
        raise ValueError("unsupported_artifact_capability")
    for key in ("mean", "scale"):
        values = artifact.get(key)
        if not isinstance(values, list) or len(values) != len(FEATURES) or any(_number(v) is None for v in values):
            raise ValueError("invalid_artifact_normalization")
    if min(artifact["scale"]) <= 0:
        raise ValueError("invalid_artifact_scale")
    models = artifact.get("models")
    if not isinstance(models, dict) or "any_editorial_edit" not in models or set(models) - set(TARGETS):
        raise ValueError("invalid_artifact_targets")
    for model in models.values():
        coefficients = model.get("coefficients", [])
        if len(coefficients) != len(FEATURES) or any(_number(v) is None for v in coefficients) or _number(model.get("intercept")) is None:
            raise ValueError("invalid_artifact_coefficients")
        calibration = model.get("calibration", {})
        if calibration.get("status") not in {"fitted_development", "uncalibrated_insufficient_groups_or_classes"}:
            raise ValueError("invalid_artifact_calibration")
        if calibration["status"] == "fitted_development" and any(_number(calibration.get(k)) is None for k in ("slope", "intercept")):
            raise ValueError("invalid_artifact_calibration")
    return artifact


def predict(artifact: dict, segments: list[dict]) -> dict:
    if artifact.get("schema") != SCHEMA or artifact.get("feature_names") != list(FEATURES):
        raise ValueError("unsupported_risk_artifact")
    vector = (np.array(source_features(segments)) - np.array(artifact["mean"])) / np.array(artifact["scale"])
    vector = np.clip(vector, -8., 8.).reshape(1, -1)
    scores = {target: float(_predict(model, vector)[0]) for target, model in artifact["models"].items()}
    routes = [(scores.get("text_or_structure_edit", 0.), "compare_audio_transcriptions"),
              (scores.get("timing_edit", 0.), "inspect_acoustic_alignment")]
    route = max(routes)[1] if max(value for value, _ in routes) >= .5 else "editorial_review"
    return {"model_id": artifact["model_id"], "risk_scores": scores, "suggested_route": route,
            "calibration": {key: value["calibration"]["status"] for key, value in artifact["models"].items()},
            "automatic_apply_allowed": False, "candidate_correctness_estimated": False}


def rank_segments(artifact: dict, segments: list[dict]) -> list[dict]:
    """Return review ordering without copying lyrics into the analytical output."""
    ranked = [{"line_index": index, **predict(artifact, [segment])} for index, segment in enumerate(segments)]
    return sorted(ranked, key=lambda row: (-row["risk_scores"]["any_editorial_edit"], row["line_index"]))
