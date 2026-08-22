"""Trusted, contrastive CTC witness for quality-v6 lyric candidates.

The caller may propose text candidates, but it cannot provide or override
their scores.  Stem and mixture are scored from locally generated emissions;
they are treated as correlated views and therefore must independently choose
the same candidate.  Missing alternatives or ties always decline.
"""
from __future__ import annotations

from io import BytesIO
import hashlib
import json
import math
import os
import re

import numpy as np


def _release() -> str:
    return str(
        os.environ.get("RELEASE")
        or os.environ.get("RAILWAY_GIT_COMMIT_SHA")
        or "unknown"
    )[:64]


def _candidate_text_key(candidate: dict) -> tuple[str, ...]:
    from ctc_align import singing_scoring_projection
    return tuple(
        singing_scoring_projection(text) for text in candidate.get("texts") or []
    )


def _vocab_sha256(dictionary: dict) -> str:
    return hashlib.sha256(json.dumps(
        dictionary, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _valid_calibration(payload: object, model_identity: dict) -> bool:
    if not isinstance(payload, dict):
        return False
    thresholds = payload.get("thresholds")
    required = {
        "stem_min_score", "stem_min_margin",
        "mix_min_score", "mix_min_margin",
    }
    if not isinstance(thresholds, dict) or not required.issubset(thresholds):
        return False
    try:
        values = {key: float(thresholds[key]) for key in required}
    except (TypeError, ValueError):
        return False
    if not all(math.isfinite(value) for value in values.values()):
        return False
    if not (
        -100.0 <= values["stem_min_score"] <= 100.0
        and -100.0 <= values["mix_min_score"] <= 100.0
        and 0.0 <= values["stem_min_margin"] <= 100.0
        and 0.0 <= values["mix_min_margin"] <= 100.0
    ):
        return False
    from transcription_quality import runtime_identity
    runtime = runtime_identity()
    return bool(
        payload.get("schema") == "quality-ctc-calibration-v1"
        and str(payload.get("calibration_id") or "").strip()
        and payload.get("model_identity") == model_identity
        and payload.get("policy_version") == "lyrics-quality-v6"
        and payload.get("release_gate_decision") == "GO"
        and payload.get("pipeline_release") == runtime["pipeline_release"]
        and payload.get("pipeline_config_fingerprint")
        == runtime["pipeline_config_fingerprint"]
        and re.fullmatch(
            r"[0-9a-f]{64}", str(payload.get("benchmark_manifest_sha256") or ""),
        )
        and re.fullmatch(
            r"[0-9a-f]{64}", str(payload.get("release_report_sha256") or ""),
        )
    )


def _load_calibration(model_identity: dict) -> dict | None:
    path = os.environ.get("QUALITY_CTC_CALIBRATION_PATH", "").strip()
    expected = os.environ.get("QUALITY_CTC_CALIBRATION_SHA256", "").strip().lower()
    if not path or len(expected) != 64:
        return None
    try:
        raw = open(path, "rb").read()
        if hashlib.sha256(raw).hexdigest() != expected:
            return None
        payload = json.loads(raw.decode("utf-8"))
        if not _valid_calibration(payload, model_identity):
            return None
        return payload
    except (OSError, ValueError, TypeError):
        return None


def _cached_emissions(
    audio_path: str, *, start: float, end: float, view: str, cache=None,
) -> tuple[dict | None, bool]:
    import ctc_align
    from quality_cache import ArtifactKind, QualityCache, QualityCacheAddress, sha256_file

    cache = cache or QualityCache()
    model, dictionary, blank_id = ctc_align._load_model()
    vocab_sha256 = _vocab_sha256(dictionary)
    audio_hash = sha256_file(audio_path)
    address = QualityCacheAddress(
        artifact=ArtifactKind.CTC,
        audio_hash=audio_hash,
        model={
            "ctc": ctc_align.MODEL_ID,
            "revision": ctc_align.MODEL_REVISION,
            "vocab_sha256": vocab_sha256,
            "blank_id": int(blank_id),
            "vocab_size": int(model.config.vocab_size),
        },
        config={
            "window": [round(float(start), 3), round(float(end), 3)],
            "frame": ctc_align.FRAME, "sample_rate": ctc_align.SR,
            "star_delta": ctc_align._star_delta(),
        },
        release=_release(),
        lineage={"view": view, "producer": "ctc_align.window_emissions:v1"},
    )
    payload = cache.get_bytes(address)
    metadata = None
    if payload:
        try:
            with np.load(BytesIO(payload), allow_pickle=False) as stored:
                emission = stored["emission"].astype(np.float32, copy=False)
                metadata = json.loads(str(stored["metadata"].item()))
            if (
                metadata.get("model_id") == ctc_align.MODEL_ID
                and metadata.get("model_revision") == ctc_align.MODEL_REVISION
                and metadata.get("vocab_sha256") == vocab_sha256
                and int(metadata.get("blank_id")) == int(blank_id)
                and int(metadata.get("vocab_size")) == int(model.config.vocab_size)
            ):
                return {
                    "emission": emission, "dictionary": dictionary,
                    "blank_id": int(blank_id),
                    "vocab_size": int(model.config.vocab_size),
                    "model_id": ctc_align.MODEL_ID,
                    "model_revision": ctc_align.MODEL_REVISION,
                    "frame_seconds": ctc_align.FRAME / ctc_align.SR,
                    "window": [float(start), float(end)],
                }, True
        except Exception:
            metadata = None
    bundle = ctc_align.window_emissions(audio_path, start, end)
    if bundle is None:
        return None, False
    try:
        output = BytesIO()
        metadata = json.dumps({
            "model_id": bundle["model_id"],
            "model_revision": bundle["model_revision"],
            "blank_id": bundle["blank_id"],
            "vocab_size": bundle["vocab_size"],
            "vocab_sha256": _vocab_sha256(bundle["dictionary"]),
        }, sort_keys=True)
        np.savez_compressed(
            output,
            emission=np.asarray(bundle["emission"], dtype=np.float32),
            metadata=np.asarray(metadata),
        )
        cache.put_bytes(
            address, output.getvalue(), content_type="application/x-npz",
        )
    except (TypeError, ValueError):
        pass
    return bundle, False


def _view_verdict(scores: list[dict], selected_id: str, *,
                  min_score: float, min_margin: float) -> dict:
    if not scores:
        return {"accepted": False, "reason": "scores_unavailable"}
    best = scores[0]
    second = scores[1] if len(scores) > 1 else None
    margin = (
        float(best["mean_score"]) - float(second["mean_score"])
        if second else 0.0
    )
    accepted = bool(
        second
        and best.get("candidate_id") == selected_id
        and float(best.get("min_score") or 0.0) >= min_score
        and margin >= min_margin
    )
    return {
        "accepted": accepted,
        "reason": "verified" if accepted else (
            "no_contrast" if second is None
            else "different_winner" if best.get("candidate_id") != selected_id
            else "score_below_floor" if float(best.get("min_score") or 0.0) < min_score
            else "margin_too_small"
        ),
        "winner": best,
        "runner_up": second,
        "margin": round(margin, 6),
    }


def verify_mapping(
    stem_path: str, mix_path: str, mapping: dict, *,
    window_start: float, window_end: float, cache=None,
    score_fn=None, emission_fn=None, structure: dict | None = None,
    calibration: dict | None = None,
) -> dict:
    """Verify selected text/topology against genuine N-best alternatives."""
    from quality_cache import sha256_file

    candidates = [
        dict(candidate) for candidate in mapping.get("phonetic_candidates") or []
        if candidate.get("candidate_id")
    ][:8]
    selected_id = str(mapping.get("selected_candidate_id") or "")
    distinct_texts = {_candidate_text_key(candidate) for candidate in candidates}
    if not selected_id or len(candidates) < 2 or len(distinct_texts) < 2:
        return {
            "accepted": False, "reason": "real_alternatives_unavailable",
            "model": "ctc_spanish_xlsr_contrastive_v1",
            "candidate_count": len(candidates),
        }
    import ctc_align
    if not re.fullmatch(r"[0-9a-f]{40}", ctc_align.MODEL_REVISION or ""):
        return {
            "accepted": False, "supported": False,
            "reason": "model_revision_unpinned",
            "model": "ctc_spanish_xlsr_contrastive_v1",
        }
    if score_fn is None:
        score_fn = ctc_align.score_structural_candidates_from_emission
    emission_fn = emission_fn or _cached_emissions
    stem_bundle, stem_hit = emission_fn(
        stem_path, start=window_start, end=window_end,
        view="vocal_stem", cache=cache,
    )
    mix_bundle, mix_hit = emission_fn(
        mix_path, start=window_start, end=window_end,
        view="original_mix", cache=cache,
    )
    if stem_bundle is None or mix_bundle is None:
        return {
            "accepted": False, "reason": "emissions_unavailable",
            "model": "ctc_spanish_xlsr_contrastive_v1",
        }
    model_identity = {
        "model_id": stem_bundle.get("model_id"),
        "model_revision": stem_bundle.get("model_revision"),
        "vocab_sha256": _vocab_sha256(stem_bundle.get("dictionary") or {}),
        "blank_id": int(stem_bundle.get("blank_id") or 0),
    }
    calibration = calibration or _load_calibration(model_identity)
    if not _valid_calibration(calibration, model_identity):
        return {
            "accepted": False, "supported": False,
            "reason": "uncalibrated",
            "model": "ctc_spanish_xlsr_contrastive_v1",
            "model_identity": model_identity,
            "views_are_correlated": True,
        }
    thresholds = (calibration or {}).get("thresholds") or {}
    stem_scores = score_fn(stem_bundle, candidates)
    mix_scores = score_fn(mix_bundle, candidates)
    stem = _view_verdict(
        stem_scores, selected_id,
        min_score=float(thresholds.get("stem_min_score", float("inf"))),
        min_margin=float(thresholds.get("stem_min_margin", float("inf"))),
    )
    mix = _view_verdict(
        mix_scores, selected_id,
        min_score=float(thresholds.get("mix_min_score", float("inf"))),
        min_margin=float(thresholds.get("mix_min_margin", float("inf"))),
    )
    supported = bool(stem.get("accepted") and mix.get("accepted"))
    accepted = bool(supported and calibration)
    stem_hash = sha256_file(stem_path) if os.path.exists(stem_path) else "unavailable"
    mix_hash = sha256_file(mix_path) if os.path.exists(mix_path) else "unavailable"
    evidence_payload = {
        "schema": "ctc-phonetic-evidence-v1",
        "selected_candidate_id": selected_id, "candidates": candidates,
        "stem_sha256": stem_hash, "mix_sha256": mix_hash,
        "structure": structure or {}, "model_identity": model_identity,
        "release": _release(), "calibration": calibration,
        "stem": stem, "mix": mix,
    }
    return {
        "accepted": accepted,
        "supported": supported,
        "reason": "verified" if accepted else (
            "uncalibrated" if supported else "correlated_views_disagree_or_weak"
        ),
        "schema": "ctc-phonetic-evidence-v1",
        "model": "ctc_spanish_xlsr_contrastive_v1",
        "model_identity": model_identity,
        "views_are_correlated": True,
        "candidate_count": len(candidates),
        "stem": stem, "mix": mix,
        "cache_hits": {"stem": stem_hit, "mix": mix_hit},
        "calibration_id": (calibration or {}).get("calibration_id"),
        "structure_fingerprint": hashlib.sha256(json.dumps(
            structure or {}, sort_keys=True, default=str,
        ).encode("utf-8")).hexdigest(),
        "evidence_sha256": hashlib.sha256(
            json.dumps(evidence_payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest(),
    }


def verify_content(
    stem_path: str, mix_path: str, structure: dict, hypotheses, *,
    window_start: float, window_end: float, cache=None,
) -> tuple[dict, dict]:
    """Production API: rebuild ranking internally; no caller-selected winner."""
    from acoustic_structure import map_content

    mapping = map_content(structure, hypotheses)
    evidence = verify_mapping(
        stem_path, mix_path, mapping, structure=structure,
        window_start=window_start, window_end=window_end, cache=cache,
    )
    mapping["phonetic_evidence"] = evidence
    mapping["phonetic_verified"] = bool(evidence.get("accepted"))
    mapping["accepted"] = bool(
        mapping.get("topology_mapping_supported") and evidence.get("accepted")
    )
    mapping["reason"] = (
        "mapped_and_phonetically_verified"
        if mapping["accepted"] else str(evidence.get("reason") or mapping.get("reason"))
    )
    return mapping, evidence
