"""Calibration contract for independent-consensus review suggestions.

This certificate is deliberately narrower than Quality v6 calibration.  It
can authorize showing a suggestion to a human reviewer, never applying a
lyric or timing mutation.  Keeping the contract separate prevents a small
consensus dataset from weakening the global Quality v6 gates.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
from collections import defaultdict
from typing import Any, Mapping, Sequence

from evidence_attestation import canonical_json, verify_artifact


OBSERVATION_SCHEMA = "independent-consensus-human-observation-v1"
CERTIFICATE_SCHEMA = "independent-consensus-review-certificate-v1"
POLICY_VERSION = "independent-consensus-review-policy-v1"

MIN_REVIEWED_WINDOWS = 50
MIN_SONGS = 10
MAX_INCORRECT = 0
MIN_SONG_BOOTSTRAP_PRECISION_LOWER_95 = 0.90
BOOTSTRAP_REPLICATES = 10_000
ALLOWED_VERDICTS = frozenset({"correct", "incorrect", "uncertain"})


def canonical_source_family(value: Any) -> str:
    """Collapse views of one model so TTA cannot certify itself."""
    normalized = str(value or "").strip().casefold()
    if "whisper" in normalized:
        return "whisper"
    if "gemini" in normalized:
        return "gemini_audio"
    if "qwen" in normalized:
        return "qwen_asr"
    if "wav2vec" in normalized or normalized.startswith("mms"):
        return "ctc_text_decoder"
    return normalized


def _sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def validate_observations(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """Validate privacy-safe, one-row-per-window human verdicts."""
    errors: list[str] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        prefix = f"observations[{index}]"
        if not isinstance(row, Mapping):
            errors.append(f"{prefix} must be an object")
            continue
        if row.get("schema") != OBSERVATION_SCHEMA:
            errors.append(f"{prefix}.schema must be {OBSERVATION_SCHEMA}")
        window_id = str(row.get("window_id") or "").strip()
        song_id = str(row.get("song_id") or "").strip()
        if not window_id:
            errors.append(f"{prefix}.window_id is required")
        elif window_id in seen:
            errors.append(f"{prefix}.window_id is duplicated")
        else:
            seen.add(window_id)
        if not song_id:
            errors.append(f"{prefix}.song_id is required")
        verdict = row.get("verdict")
        if verdict not in ALLOWED_VERDICTS:
            errors.append(f"{prefix}.verdict is invalid")
        families = row.get("source_families")
        if (
            not isinstance(families, list)
            or len({canonical_source_family(item) for item in families if item}) < 2
        ):
            errors.append(f"{prefix}.source_families requires two independent families")
        for field in ("candidate_sha256", "audio_window_sha256"):
            if not _is_sha256(row.get(field)):
                errors.append(f"{prefix}.{field} must be SHA-256")
    return errors


def _song_bootstrap_lower_bound(
    judged_by_song: Mapping[str, Sequence[bool]],
    *,
    replicates: int = BOOTSTRAP_REPLICATES,
    seed: str = POLICY_VERSION,
) -> float:
    """Return the 5th percentile after resampling whole songs.

    Sampling songs, rather than windows, prevents one chorus with many nearly
    identical windows from pretending to be broad evidence.
    """
    songs = sorted(judged_by_song)
    if not songs or replicates <= 0:
        return 0.0
    rng = random.Random(seed)
    estimates: list[float] = []
    for _ in range(replicates):
        chosen = [songs[rng.randrange(len(songs))] for _ in songs]
        outcomes = [outcome for song in chosen for outcome in judged_by_song[song]]
        estimates.append(sum(outcomes) / len(outcomes) if outcomes else 0.0)
    estimates.sort()
    # Conservative empirical one-sided 95% lower percentile.
    return estimates[max(0, math.floor(0.05 * len(estimates)) - 1)]


def evaluate_consensus_review_gate(
    rows: Sequence[Mapping[str, Any]],
    *,
    bootstrap_replicates: int = BOOTSTRAP_REPLICATES,
) -> dict[str, Any]:
    """Evaluate the pre-declared gate without granting runtime authority."""
    errors = validate_observations(rows)
    judged_by_song: dict[str, list[bool]] = defaultdict(list)
    uncertain = 0
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        verdict = row.get("verdict")
        if verdict == "uncertain":
            uncertain += 1
            continue
        if verdict in {"correct", "incorrect"}:
            judged_by_song[str(row.get("song_id") or "")].append(verdict == "correct")
    reviewed = sum(len(values) for values in judged_by_song.values())
    correct = sum(sum(values) for values in judged_by_song.values())
    incorrect = reviewed - correct
    songs = len(judged_by_song)
    lower = _song_bootstrap_lower_bound(
        judged_by_song, replicates=bootstrap_replicates,
    )
    blockers = []
    if errors:
        blockers.append("invalid_observations")
    if reviewed < MIN_REVIEWED_WINDOWS:
        blockers.append("insufficient_reviewed_windows")
    if songs < MIN_SONGS:
        blockers.append("insufficient_songs")
    if incorrect > MAX_INCORRECT:
        blockers.append("incorrect_consensus_approved")
    if lower < MIN_SONG_BOOTSTRAP_PRECISION_LOWER_95:
        blockers.append("song_bootstrap_precision_below_target")
    return {
        "schema": CERTIFICATE_SCHEMA,
        "policy_version": POLICY_VERSION,
        "eligible_for_signed_certificate": not blockers,
        "review_proposals_only": True,
        "automatic_apply_allowed": False,
        "runtime_authorization": False,
        "reviewed_windows": reviewed,
        "correct": correct,
        "incorrect": incorrect,
        "uncertain_excluded": uncertain,
        "songs": songs,
        "precision": correct / reviewed if reviewed else 0.0,
        "song_bootstrap_precision_lower_95": lower,
        "thresholds": {
            "minimum_reviewed_windows": MIN_REVIEWED_WINDOWS,
            "minimum_songs": MIN_SONGS,
            "maximum_incorrect": MAX_INCORRECT,
            "minimum_song_bootstrap_precision_lower_95": (
                MIN_SONG_BOOTSTRAP_PRECISION_LOWER_95
            ),
            "bootstrap_replicates": bootstrap_replicates,
        },
        "observations_sha256": _sha256(list(rows)),
        "blockers": blockers,
        "validation_errors": errors,
    }


def validate_signed_certificate(
    artifact: Mapping[str, Any] | None,
    *,
    public_keys_env: str = "QUALITY_CONSENSUS_CERTIFICATE_PUBLIC_KEYS",
) -> list[str]:
    """Validate the eventual certificate; never authorize auto-application."""
    if not isinstance(artifact, Mapping):
        return ["certificate must be an object"]
    errors: list[str] = []
    if artifact.get("schema") != CERTIFICATE_SCHEMA:
        errors.append(f"schema must be {CERTIFICATE_SCHEMA}")
    if artifact.get("policy_version") != POLICY_VERSION:
        errors.append(f"policy_version must be {POLICY_VERSION}")
    if artifact.get("status") != "calibrated":
        errors.append("status must be calibrated")
    if artifact.get("review_proposals_only") is not True:
        errors.append("review_proposals_only must be true")
    if artifact.get("automatic_apply_allowed") is not False:
        errors.append("automatic_apply_allowed must be false")
    if artifact.get("runtime_authorization") is not False:
        errors.append("runtime_authorization must be false")
    thresholds = artifact.get("thresholds") or {}
    expected = {
        "minimum_reviewed_windows": MIN_REVIEWED_WINDOWS,
        "minimum_songs": MIN_SONGS,
        "maximum_incorrect": MAX_INCORRECT,
        "minimum_song_bootstrap_precision_lower_95": (
            MIN_SONG_BOOTSTRAP_PRECISION_LOWER_95
        ),
    }
    if any(thresholds.get(key) != value for key, value in expected.items()):
        errors.append("thresholds do not match the pre-declared policy")
    if int(artifact.get("reviewed_windows") or 0) < MIN_REVIEWED_WINDOWS:
        errors.append("insufficient reviewed windows")
    if int(artifact.get("songs") or 0) < MIN_SONGS:
        errors.append("insufficient songs")
    if int(artifact.get("incorrect") or 0) != 0:
        errors.append("certificate contains an incorrect approval")
    lower = artifact.get("song_bootstrap_precision_lower_95")
    if not isinstance(lower, (int, float)) or float(lower) < MIN_SONG_BOOTSTRAP_PRECISION_LOWER_95:
        errors.append("song bootstrap precision is below target")
    if not _is_sha256(artifact.get("observations_sha256")):
        errors.append("observations_sha256 must be SHA-256")
    verified, reason = verify_artifact(dict(artifact), public_keys_env)
    if not verified:
        errors.append(f"certificate attestation rejected: {reason}")
    return errors
