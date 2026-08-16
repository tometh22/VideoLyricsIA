"""Interpretable, calibrated shadow predictor for correction risk.

The artifact is a signed Bernoulli model over privacy-safe features. It can
recommend an analysis route in shadow, but this module has no segment mutation
API by design.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any


def feature_tokens(features: dict) -> list[str]:
    tokens = []
    for key, value in sorted((features or {}).items()):
        if isinstance(value, bool) and value:
            tokens.append(f"{key}=true")
        elif key in {"timing_source", "quality_decision", "language"} and value:
            tokens.append(f"{key}={str(value)[:64]}")
    return tokens


def _raw_probability(model: dict, tokens: set[str]) -> float:
    prior = min(1 - 1e-6, max(1e-6, float(model["prior"])))
    log_odds = math.log(prior / (1 - prior))
    for token, likelihood in (model.get("features") or {}).items():
        py = min(1 - 1e-6, max(1e-6, float(likelihood["positive"])))
        pn = min(1 - 1e-6, max(1e-6, float(likelihood["negative"])))
        if token in tokens:
            log_odds += math.log(py / pn)
        else:
            log_odds += math.log((1 - py) / (1 - pn))
    if log_odds >= 0:
        return 1 / (1 + math.exp(-log_odds))
    exp_value = math.exp(log_odds)
    return exp_value / (1 + exp_value)


def _calibrate(model: dict, probability: float) -> float:
    bins = model.get("calibration") or []
    if bins and probability < float(bins[0]["min"]):
        return float(bins[0]["rate"])
    for row in bins:
        if float(row["min"]) <= probability <= float(row["max"]):
            return float(row["rate"])
    if bins and probability > float(bins[-1]["max"]):
        return float(bins[-1]["rate"])
    return probability


def predict(artifact: dict, features: dict) -> dict:
    tokens = set(feature_tokens(features))
    probabilities = {}
    for category, model in (artifact.get("models") or {}).items():
        raw = _raw_probability(model, tokens)
        probabilities[category] = round(_calibrate(model, raw), 6)
    ranked = sorted(probabilities, key=probabilities.get, reverse=True)
    route = "baseline"
    if any(probabilities.get(key, 0) >= 0.5 for key in ("missing_event", "missing_vocalization")):
        route = "mix_witness_second_asr"
    elif any(probabilities.get(key, 0) >= 0.5 for key in ("timing_onset", "timing_end")):
        route = "ctc_confirmation"
    elif any(probabilities.get(key, 0) >= 0.5 for key in ("split", "merge")):
        route = "acoustic_dp"
    return {
        "probabilities": probabilities,
        "top_categories": ranked[:5], "suggested_route": route,
        "mutated_segments": False, "mode": "shadow",
    }


def load_verified_artifact() -> tuple[dict | None, str]:
    if os.environ.get("QUALITY_LEARNING_MODEL_SHADOW_ENABLED", "0").strip().lower() not in {
        "1", "true", "yes", "on",
    }:
        return None, "disabled"
    path = os.environ.get("QUALITY_LEARNING_MODEL_PATH", "").strip()
    if not path:
        return None, "missing_path"
    try:
        artifact = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, "unreadable_artifact"
    from evidence_attestation import verify_artifact
    verified, reason = verify_artifact(
        artifact, "QUALITY_LEARNING_MODEL_PUBLIC_KEYS",
    )
    if not verified:
        return None, reason
    if artifact.get("schema") != "quality-learning-model-v1":
        return None, "unsupported_schema"
    if artifact.get("mode") != "shadow" or artifact.get("writes_lyrics") is not False:
        return None, "unsafe_model_capability"
    return artifact, "verified"


def shadow_prediction(job: Any) -> dict:
    artifact, reason = load_verified_artifact()
    if artifact is None:
        return {"available": False, "reason": reason, "mutated_segments": False}
    from correction_learning import privacy_safe_features
    return {
        "available": True, "artifact_id": artifact.get("artifact_id"),
        **predict(artifact, privacy_safe_features(job)),
    }


def shadow_prediction_for_quality(quality: dict, timing_source: str) -> dict:
    """Predict from the candidate quality payload without persisting raw data."""
    return shadow_prediction(SimpleNamespace(
        transcription_quality=quality,
        timing_source=timing_source,
    ))
