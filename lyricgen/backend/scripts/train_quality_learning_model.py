#!/usr/bin/env python3
"""Train a signed, interpretable correction-risk model after evidence gates."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from evidence_attestation import sign_artifact, write_json_exclusive  # noqa: E402
from quality_learning_model import _raw_probability, feature_tokens  # noqa: E402
from correction_learning import (  # noqa: E402
    _normalise_entity_identifier, current_hmac_key_id,
)


def _split(secret: str, group: str) -> str:
    value = int(hmac.new(secret.encode(), group.encode(), hashlib.sha256).hexdigest()[:8], 16) % 10
    return "train" if value < 6 else "calibration" if value < 8 else "evaluation"


def _split_group(job, observation) -> str:
    """Keep every song/master from one artist in exactly one data partition."""
    artist = _normalise_entity_identifier(getattr(job, "artist", "") or "")
    if artist:
        return f"artist\x1f{artist}"
    song = _normalise_entity_identifier(getattr(job, "song_title", "") or "")
    return f"fallback\x1f{song}\x1f{observation.audio_hash or observation.identity_hash}"


def _fit(rows: list[dict], category: str, vocabulary: list[str]) -> dict:
    positive = [row for row in rows if row["label"]]
    negative = [row for row in rows if not row["label"]]
    return {
        "prior": (len(positive) + 1) / (len(rows) + 2),
        "features": {
            token: {
                "positive": (sum(token in row["tokens"] for row in positive) + 1) / (len(positive) + 2),
                "negative": (sum(token in row["tokens"] for row in negative) + 1) / (len(negative) + 2),
            }
            for token in vocabulary
        },
        "positive_train": len(positive), "negative_train": len(negative),
    }


def _calibration_bins(model: dict, rows: list[dict]) -> list[dict]:
    scored = sorted((_raw_probability(model, row["tokens"]), row["label"]) for row in rows)
    if not scored:
        return []
    bins = []
    width = max(1, math.ceil(len(scored) / 10))
    for index in range(0, len(scored), width):
        chunk = scored[index:index + width]
        bins.append({
            "min": chunk[0][0], "max": chunk[-1][0],
            "rate": (sum(label for _score, label in chunk) + 1) / (len(chunk) + 2),
            "count": len(chunk),
        })
    return bins


def _evaluate(model: dict, rows: list[dict]) -> dict:
    if not rows:
        return {"ece": 1.0, "brier": 1.0, "baseline_brier": 1.0}
    predictions = []
    for row in rows:
        raw = _raw_probability(model, row["tokens"])
        calibrated = next((
            float(item["rate"]) for item in model["calibration"]
            if float(item["min"]) <= raw <= float(item["max"])
        ), raw)
        predictions.append((calibrated, float(row["label"])))
    prevalence = sum(label for _p, label in predictions) / len(predictions)
    brier = sum((p - label) ** 2 for p, label in predictions) / len(predictions)
    baseline = sum((prevalence - label) ** 2 for _p, label in predictions) / len(predictions)
    # Equal-width ECE on the untouched evaluation set.
    ece = 0.0
    for bin_index in range(10):
        low, high = bin_index / 10, (bin_index + 1) / 10
        bucket = [
            (p, y) for p, y in predictions
            if low <= p < high or (bin_index == 9 and p == high)
        ]
        if bucket:
            ece += len(bucket) / len(predictions) * abs(
                sum(p for p, _y in bucket) / len(bucket)
                - sum(y for _p, y in bucket) / len(bucket)
            )
    return {"ece": round(ece, 6), "brier": round(brier, 6), "baseline_brier": round(baseline, 6)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    split_key = os.environ.get("QUALITY_LEARNING_SPLIT_KEY", "").strip()
    private_key = os.environ.get("QUALITY_LEARNING_MODEL_PRIVATE_KEY", "").strip()
    key_id = os.environ.get("QUALITY_LEARNING_MODEL_KEY_ID", "").strip()
    if not split_key or not private_key or not key_id:
        parser.error("split and Ed25519 signing keys are required")

    from database import CorrectionObservation, Job, SessionLocal
    db = SessionLocal()
    try:
        observations = db.query(CorrectionObservation).filter(
            CorrectionObservation.label_tier == "trusted",
            CorrectionObservation.invalidated_at.is_(None),
            CorrectionObservation.hmac_key_id == current_hmac_key_id(),
        ).all()
        if len(observations) < 500:
            parser.error("at least 500 trusted observations are required")
        jobs = {row.job_id: row for row in db.query(Job).filter(
            Job.job_id.in_([item.job_id for item in observations]),
        ).all()}
    finally:
        db.close()
    counts = Counter(key for row in observations for key, value in (row.categories or {}).items() if value)
    categories = sorted(key for key, count in counts.items() if count >= 100)
    if not categories:
        parser.error("no category has 100 positive trusted observations")
    vocabulary = sorted({token for row in observations for token in feature_tokens(row.features or {})})
    models = {}
    for category in categories:
        rows = []
        for observation in observations:
            job = jobs.get(observation.job_id)
            group = _split_group(job, observation)
            rows.append({
                "tokens": set(feature_tokens(observation.features or {})),
                "label": bool((observation.categories or {}).get(category)),
                "split": _split(split_key, group),
            })
        train = [row for row in rows if row["split"] == "train"]
        calibration = [row for row in rows if row["split"] == "calibration"]
        evaluation = [row for row in rows if row["split"] == "evaluation"]
        if sum(row["label"] for row in train) < 50 or not calibration or not evaluation:
            continue
        model = _fit(train, category, vocabulary)
        model["calibration"] = _calibration_bins(model, calibration)
        model["evaluation"] = _evaluate(model, evaluation)
        if model["evaluation"]["ece"] <= 0.10 and model["evaluation"]["brier"] <= model["evaluation"]["baseline_brier"]:
            models[category] = model
    if not models:
        parser.error("no category passed calibration and holdout gates")
    artifact = sign_artifact({
        "schema": "quality-learning-model-v1", "artifact_id": str(uuid.uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": "shadow", "writes_lyrics": False,
        "trusted_observations": len(observations), "models": models,
        "feature_vocabulary": vocabulary,
        "split": {"train": 0.6, "calibration": 0.2, "evaluation": 0.2, "grouped": True},
    }, private_key, key_id)
    write_json_exclusive(args.output, artifact)
    print(json.dumps({"output": str(args.output), "models": sorted(models)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
