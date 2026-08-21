"""Offline, fail-closed contracts for transcription quality v6.

This module deliberately has no runtime mutation hook.  It validates the
licensed dataset, signed calibration evidence, selective-action evidence and
the conformal prediction shape that a future runtime integration may consume.
Passing these checks means "eligible for an offline experiment", never
"authorized to edit lyrics".
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
import os

from evidence_attestation import canonical_json, verify_artifact


POLICY_VERSION = "lyrics-quality-v6"
DATASET_SCHEMA = "lyrics-quality-v6-dataset-manifest-v1"
CALIBRATION_SCHEMA = "lyrics-quality-v6-calibration-v1"
TRAINING_REPORT_SCHEMA = "lyrics-quality-v6-phone-event-training-v1"
PREDICTION_CERTIFICATION_SCHEMA = "lyrics-quality-v6-prediction-certification-v1"

SPLITS = ("training", "regression", "calibration", "temporal")
CATEGORIES = ("live", "studio", "adversarial")
IDENTITY_FIELDS = ("artist_sha256", "song_sha256", "master_sha256", "audio_sha256")
RIGHTS_BASES = ("contract", "owned", "licensed", "public_domain", "synthetic")

# Product-level evidence requirements.  The training requirements are the
# minimum for an export candidate, not a statement that this volume guarantees
# model quality.
DATA_REQUIREMENTS: dict[str, Any] = {
    "training_cases_min": 100,
    "training_hours_min": 25.0,
    "training_events_min": 2_000,
    "training_live_fraction_min": 0.50,
    "regression_cases": 50,
    "regression_categories": {"live": 20, "studio": 20, "adversarial": 10},
    "calibration_cases_min": 300,
    "temporal_cases_min": 150,
}

# One-sided 95% Wilson gates.  The minimum reviewed counts prevent a tiny,
# perfect sample from looking production-ready.
ACTION_GATES: dict[str, dict[str, float | int]] = {
    "suggestion": {"minimum_reviewed": 300, "minimum_lower_bound": 0.95},
    "timing_reversible": {"minimum_reviewed": 539, "minimum_lower_bound": 0.995},
    "content_reversible": {"minimum_reviewed": 539, "minimum_lower_bound": 0.995},
    "structural": {"minimum_reviewed": 3_000, "minimum_lower_bound": 0.999},
}

CONFORMAL_MIN_EXAMPLES = 300
CONFORMAL_MIN_COVERAGE = 0.99
MAX_ONSET_INTERVAL_WIDTH_S = 0.40
MAX_END_INTERVAL_WIDTH_S = 0.70


def artifact_sha256(value: Any) -> str:
    """Hash an in-memory JSON artifact exactly as v6 bindings expect."""
    return hashlib.sha256(canonical_json(value)).hexdigest()


def split_sha256(manifest: Mapping[str, Any], split: str) -> str:
    """Bind evidence to stable case identities, gold and rights for one split."""
    rows = []
    for entry in manifest.get("entries") or []:
        if not isinstance(entry, Mapping) or entry.get("split") != split:
            continue
        annotations = entry.get("annotations") if isinstance(entry.get("annotations"), Mapping) else {}
        rights = entry.get("license") if isinstance(entry.get("license"), Mapping) else {}
        rows.append({
            "case_id": entry.get("case_id"),
            "identity": entry.get("identity"),
            "annotation_sha256": annotations.get("artifact_sha256"),
            "rights_sha256": rights.get("evidence_sha256"),
        })
    rows.sort(key=lambda row: str(row.get("case_id") or ""))
    return artifact_sha256(rows)


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_utc_timestamp(value: Any) -> bool:
    if not _nonempty(value):
        return False
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def wilson_lower_bound(successes: int, total: int, *, z: float = 1.6448536269514722) -> float:
    """Return the one-sided 95% Wilson lower confidence bound."""
    if (
        isinstance(successes, bool)
        or isinstance(total, bool)
        or not isinstance(successes, int)
        or not isinstance(total, int)
        or total <= 0
        or successes < 0
        or successes > total
    ):
        return 0.0
    observed = successes / total
    denominator = 1.0 + (z * z) / total
    centre = observed + (z * z) / (2.0 * total)
    margin = z * math.sqrt(
        observed * (1.0 - observed) / total + (z * z) / (4.0 * total * total)
    )
    return max(0.0, (centre - margin) / denominator)


def evaluate_action_gate(
    action: str,
    *,
    correct: int,
    reviewed: int,
    catastrophic: int = 0,
) -> dict[str, Any]:
    """Evaluate an action independently; unknown actions always abstain."""
    target = ACTION_GATES.get(action)
    lower = wilson_lower_bound(correct, reviewed)
    valid_counts = bool(
        isinstance(catastrophic, int)
        and not isinstance(catastrophic, bool)
        and 0 <= catastrophic <= reviewed
        and isinstance(correct, int)
        and not isinstance(correct, bool)
        and isinstance(reviewed, int)
        and not isinstance(reviewed, bool)
        and 0 <= correct <= reviewed
    )
    passed = bool(
        target
        and valid_counts
        and reviewed >= int(target["minimum_reviewed"])
        and catastrophic == 0
        and lower >= float(target["minimum_lower_bound"])
    )
    blockers: list[str] = []
    if target is None:
        blockers.append("unknown_action")
    elif not valid_counts:
        blockers.append("invalid_counts")
    else:
        if reviewed < int(target["minimum_reviewed"]):
            blockers.append("insufficient_reviewed_actions")
        if catastrophic:
            blockers.append("catastrophic_action")
        if lower < float(target["minimum_lower_bound"]):
            blockers.append("wilson_lower_bound_below_target")
    return {
        "action": action,
        "passed": passed,
        "correct": correct,
        "reviewed": reviewed,
        "catastrophic": catastrophic,
        "precision": correct / reviewed if valid_counts and reviewed > 0 else 0.0,
        "wilson_lower_95": lower,
        "target": dict(target) if target else None,
        "blockers": blockers,
    }


def summarize_dataset(entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Produce the only count summary accepted by the v6 validator."""
    split_counts = Counter(str(row.get("split") or "") for row in entries)
    category_counts: dict[str, Counter[str]] = defaultdict(Counter)
    training_seconds = 0.0
    training_events = 0
    for row in entries:
        split = str(row.get("split") or "")
        category_counts[split][str(row.get("category") or "")] += 1
        if split == "training":
            example = (
                row.get("training_example")
                if isinstance(row.get("training_example"), Mapping)
                else {}
            )
            duration = example.get("duration_seconds")
            events = example.get("event_count")
            if _finite(duration) and float(duration) > 0:
                training_seconds += float(duration)
            if isinstance(events, int) and not isinstance(events, bool) and events > 0:
                training_events += events
    training_count = split_counts.get("training", 0)
    training_live = category_counts["training"].get("live", 0)
    return {
        "split_counts": {split: split_counts.get(split, 0) for split in SPLITS},
        "category_counts": {
            split: {category: category_counts[split].get(category, 0) for category in CATEGORIES}
            for split in SPLITS
        },
        "training_hours": round(training_seconds / 3600.0, 6),
        "training_events": training_events,
        "training_live_fraction": training_live / training_count if training_count else 0.0,
    }


def dataset_adequacy(summary: Mapping[str, Any]) -> dict[str, Any]:
    """Apply immutable corpus-volume requirements to a derived summary."""
    counts = summary.get("split_counts") if isinstance(summary.get("split_counts"), Mapping) else {}
    categories = (
        summary.get("category_counts")
        if isinstance(summary.get("category_counts"), Mapping)
        else {}
    )
    regression_categories = (
        categories.get("regression")
        if isinstance(categories.get("regression"), Mapping)
        else {}
    )
    checks = {
        "training_cases": int(counts.get("training") or 0)
        >= DATA_REQUIREMENTS["training_cases_min"],
        "training_hours": float(summary.get("training_hours") or 0.0)
        >= DATA_REQUIREMENTS["training_hours_min"],
        "training_events": int(summary.get("training_events") or 0)
        >= DATA_REQUIREMENTS["training_events_min"],
        "training_live_fraction": float(summary.get("training_live_fraction") or 0.0)
        >= DATA_REQUIREMENTS["training_live_fraction_min"],
        "regression_cases": int(counts.get("regression") or 0)
        == DATA_REQUIREMENTS["regression_cases"],
        "calibration_cases": int(counts.get("calibration") or 0)
        >= DATA_REQUIREMENTS["calibration_cases_min"],
        "temporal_cases": int(counts.get("temporal") or 0)
        >= DATA_REQUIREMENTS["temporal_cases_min"],
    }
    for category, required in DATA_REQUIREMENTS["regression_categories"].items():
        checks[f"regression_{category}"] = int(regression_categories.get(category) or 0) == required
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "blockers": sorted(name for name, passed in checks.items() if not passed),
    }


def _validate_contract(contract: Any, errors: list[str]) -> None:
    if not isinstance(contract, Mapping):
        errors.append("contract must be an object")
        return
    if contract.get("purpose") != "offline_phone_event_training_calibration":
        errors.append("contract.purpose must be offline_phone_event_training_calibration")
    for field in (
        "tenant_opt_in_required",
        "ambiguous_rights_excluded",
        "raw_content_tenant_scoped",
        "revocation_supported",
    ):
        if contract.get(field) is not True:
            errors.append(f"contract.{field} must be exactly true")
    if not isinstance(contract.get("contains_customer_audio"), bool):
        errors.append("contract.contains_customer_audio must be boolean")
    if not _nonempty(contract.get("legal_review_id")):
        errors.append("contract.legal_review_id is required")
    if not _nonempty(contract.get("revocation_process")):
        errors.append("contract.revocation_process is required")


def _validate_entry(entry: Any, index: int, errors: list[str]) -> None:
    prefix = f"entries[{index}]"
    if not isinstance(entry, Mapping):
        errors.append(f"{prefix} must be an object")
        return
    if not _nonempty(entry.get("case_id")):
        errors.append(f"{prefix}.case_id is required")
    if entry.get("split") not in SPLITS:
        errors.append(f"{prefix}.split must be one of {', '.join(SPLITS)}")
    if entry.get("category") not in CATEGORIES:
        errors.append(f"{prefix}.category must be one of {', '.join(CATEGORIES)}")

    identity = entry.get("identity")
    if not isinstance(identity, Mapping):
        errors.append(f"{prefix}.identity must be an object")
        identity = {}
    for field in IDENTITY_FIELDS:
        if not _is_sha256(identity.get(field)):
            errors.append(f"{prefix}.identity.{field} must be SHA-256")

    audio = entry.get("audio")
    if not isinstance(audio, Mapping):
        errors.append(f"{prefix}.audio must be an object")
        audio = {}
    if not _is_sha256(audio.get("sha256")):
        errors.append(f"{prefix}.audio.sha256 must be SHA-256")
    if identity.get("audio_sha256") != audio.get("sha256"):
        errors.append(f"{prefix}.audio.sha256 must equal identity.audio_sha256")
    if not _finite(audio.get("duration_seconds")) or float(audio.get("duration_seconds") or 0) <= 0:
        errors.append(f"{prefix}.audio.duration_seconds must be positive and finite")
    if not _nonempty(audio.get("storage_uri")):
        errors.append(f"{prefix}.audio.storage_uri is required")

    annotations = entry.get("annotations")
    if not isinstance(annotations, Mapping):
        errors.append(f"{prefix}.annotations must be an object")
        annotations = {}
    if annotations.get("status") != "adjudicated":
        errors.append(f"{prefix}.annotations.status must be adjudicated")
    if (
        not isinstance(annotations.get("annotator_count"), int)
        or isinstance(annotations.get("annotator_count"), bool)
        or int(annotations.get("annotator_count") or 0) < 2
    ):
        errors.append(f"{prefix}.annotations.annotator_count must be at least 2")
    if not _is_sha256(annotations.get("adjudicator_id_sha256")):
        errors.append(f"{prefix}.annotations.adjudicator_id_sha256 must be SHA-256")
    if not _is_sha256(annotations.get("artifact_sha256")):
        errors.append(f"{prefix}.annotations.artifact_sha256 must be SHA-256")
    if annotations.get("hierarchical") is not True:
        errors.append(f"{prefix}.annotations.hierarchical must be exactly true")
    if (
        not isinstance(annotations.get("event_count"), int)
        or isinstance(annotations.get("event_count"), bool)
        or int(annotations.get("event_count") or 0) <= 0
    ):
        errors.append(f"{prefix}.annotations.event_count must be a positive integer")

    rights = entry.get("license")
    if not isinstance(rights, Mapping):
        errors.append(f"{prefix}.license must be an object")
        rights = {}
    for field in ("license_id", "license_name", "license_uri"):
        if not _nonempty(rights.get(field)):
            errors.append(f"{prefix}.license.{field} is required")
    if rights.get("rights_basis") not in RIGHTS_BASES:
        errors.append(f"{prefix}.license.rights_basis must be an allowed value")
    if not _is_sha256(rights.get("evidence_sha256")):
        errors.append(f"{prefix}.license.evidence_sha256 must be SHA-256")
    for field in (
        "commercial_use_allowed",
        "model_training_allowed",
        "global_training_allowed",
        "derivatives_allowed",
    ):
        if rights.get(field) is not True:
            errors.append(f"{prefix}.license.{field} must be exactly true")
    expires_at = rights.get("expires_at")
    if expires_at is not None and not _is_utc_timestamp(expires_at):
        errors.append(f"{prefix}.license.expires_at must be null or timezone-aware ISO-8601")
    elif expires_at is not None:
        parsed_expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        if parsed_expiry.astimezone(timezone.utc) <= datetime.now(timezone.utc):
            errors.append(f"{prefix}.license.expires_at has passed")

    if entry.get("split") == "training":
        example = entry.get("training_example")
        if not isinstance(example, Mapping):
            errors.append(f"{prefix}.training_example must be an object for training rows")
        else:
            if example.get("format") != "phone-event-npz-v1":
                errors.append(f"{prefix}.training_example.format must be phone-event-npz-v1")
            if not _nonempty(example.get("path")):
                errors.append(f"{prefix}.training_example.path is required")
            if not _is_sha256(example.get("sha256")):
                errors.append(f"{prefix}.training_example.sha256 must be SHA-256")
            if example.get("sample_rate_hz") != 16_000:
                errors.append(f"{prefix}.training_example.sample_rate_hz must be 16000")
            example_duration = example.get("duration_seconds")
            if (
                not _finite(example_duration)
                or float(example_duration or 0) <= 0
                or float(example_duration or 0) > float(audio.get("duration_seconds") or 0)
            ):
                errors.append(
                    f"{prefix}.training_example.duration_seconds must be positive and no longer than audio"
                )
            example_events = example.get("event_count")
            if (
                not isinstance(example_events, int)
                or isinstance(example_events, bool)
                or example_events <= 0
                or example_events > int(annotations.get("event_count") or 0)
            ):
                errors.append(
                    f"{prefix}.training_example.event_count must be positive and no greater than annotations"
                )


def validate_dataset_manifest(
    manifest: Any,
    *,
    require_signature: bool = True,
    public_keys_env: str = "QUALITY_V6_DATASET_PUBLIC_KEYS",
    require_adequate: bool = True,
) -> list[str]:
    """Return all dataset errors; any error means the corpus is unusable."""
    if not isinstance(manifest, Mapping):
        return ["manifest must be an object"]
    errors: list[str] = []
    if manifest.get("schema") != DATASET_SCHEMA:
        errors.append(f"schema must be {DATASET_SCHEMA}")
    if manifest.get("policy_version") != POLICY_VERSION:
        errors.append(f"policy_version must be {POLICY_VERSION}")
    if manifest.get("status") not in ("draft", "ready"):
        errors.append("status must be draft or ready")
    if not _nonempty(manifest.get("dataset_id")):
        errors.append("dataset_id is required")
    if not _is_utc_timestamp(manifest.get("created_at")):
        errors.append("created_at must be timezone-aware ISO-8601")
    _validate_contract(manifest.get("contract"), errors)

    entries = manifest.get("entries")
    if not isinstance(entries, list):
        errors.append("entries must be a list")
        entries = []
    seen_cases: set[str] = set()
    identities: dict[str, dict[str, str]] = {field: {} for field in IDENTITY_FIELDS}
    for index, entry in enumerate(entries):
        _validate_entry(entry, index, errors)
        if not isinstance(entry, Mapping):
            continue
        case_id = entry.get("case_id")
        if _nonempty(case_id):
            if str(case_id) in seen_cases:
                errors.append(f"entries[{index}]: duplicate case_id {case_id}")
            seen_cases.add(str(case_id))
        split = entry.get("split")
        identity = entry.get("identity") if isinstance(entry.get("identity"), Mapping) else {}
        for field in IDENTITY_FIELDS:
            value = identity.get(field)
            if not (_is_sha256(value) and split in SPLITS):
                continue
            prior = identities[field].get(str(value))
            if prior is not None and prior != split:
                errors.append(
                    f"entries[{index}]: {field} leakage across {prior}/{split}"
                )
            else:
                identities[field][str(value)] = str(split)

    summary = summarize_dataset(entries)
    if manifest.get("summary") != summary:
        errors.append("summary must exactly equal the summary derived from entries")
    adequacy = dataset_adequacy(summary)
    if require_adequate and not adequacy["passed"]:
        errors.extend(f"inadequate:{blocker}" for blocker in adequacy["blockers"])
    if require_signature:
        verified, reason = verify_artifact(dict(manifest), public_keys_env)
        if not verified:
            errors.append(f"dataset attestation rejected: {reason}")
        if manifest.get("status") != "ready":
            errors.append("signed dataset status must be ready")
    return errors


def _validate_conformal_block(name: str, block: Any, errors: list[str]) -> None:
    prefix = f"conformal.{name}"
    if not isinstance(block, Mapping):
        errors.append(f"{prefix} must be an object")
        return
    allowed_methods = {
        "cardinality": ("split_conformal",),
        "content": ("aps", "raps"),
        "timing": ("conformalized_quantile",),
    }
    if block.get("method") not in allowed_methods[name]:
        errors.append(f"{prefix}.method is unsupported for {name}")
    if int(block.get("calibration_examples") or 0) < CONFORMAL_MIN_EXAMPLES:
        errors.append(f"{prefix}.calibration_examples must be at least {CONFORMAL_MIN_EXAMPLES}")
    total = block.get("empirical_total")
    covered = block.get("empirical_covered")
    if (
        not isinstance(total, int)
        or isinstance(total, bool)
        or not isinstance(covered, int)
        or isinstance(covered, bool)
        or total < CONFORMAL_MIN_EXAMPLES
        or covered < 0
        or covered > total
    ):
        errors.append(f"{prefix} empirical coverage counts are invalid")
        empirical = 0.0
    else:
        empirical = covered / total
    target = block.get("target_coverage")
    if not _finite(target) or not CONFORMAL_MIN_COVERAGE <= float(target) < 1.0:
        errors.append(f"{prefix}.target_coverage must be in [{CONFORMAL_MIN_COVERAGE}, 1)")
    elif empirical < float(target):
        errors.append(f"{prefix}.empirical_coverage is below target")
    expected_contract = "singleton_only" if name in ("cardinality", "content") else "bounded_intervals"
    if block.get("decision_contract") != expected_contract:
        errors.append(f"{prefix}.decision_contract must be {expected_contract}")
    if name == "timing" and block.get("coverage_scope") != "joint_onset_end":
        errors.append(f"{prefix}.coverage_scope must be joint_onset_end")


def validate_calibration_artifact(
    artifact: Any,
    dataset_manifest: Any,
    *,
    calibration_public_keys_env: str = "QUALITY_V6_CALIBRATION_PUBLIC_KEYS",
    dataset_public_keys_env: str = "QUALITY_V6_DATASET_PUBLIC_KEYS",
) -> list[str]:
    """Validate signed offline calibration; never grants runtime authority."""
    errors = validate_dataset_manifest(
        dataset_manifest,
        require_signature=True,
        public_keys_env=dataset_public_keys_env,
        require_adequate=True,
    )
    errors = [f"dataset:{error}" for error in errors]
    if not isinstance(artifact, Mapping):
        return errors + ["calibration artifact must be an object"]
    if artifact.get("schema") != CALIBRATION_SCHEMA:
        errors.append(f"schema must be {CALIBRATION_SCHEMA}")
    if artifact.get("policy_version") != POLICY_VERSION:
        errors.append(f"policy_version must be {POLICY_VERSION}")
    if artifact.get("status") != "offline_calibrated":
        errors.append("status must be offline_calibrated")
    if artifact.get("offline_only") is not True:
        errors.append("offline_only must be exactly true")
    if artifact.get("runtime_authorization") is not False:
        errors.append("runtime_authorization must be exactly false")
    if artifact.get("automatic_apply_allowed") is not False:
        errors.append("automatic_apply_allowed must be exactly false")
    if artifact.get("dataset_manifest_sha256") != artifact_sha256(dataset_manifest):
        errors.append("dataset_manifest_sha256 mismatch")
    if artifact.get("dataset_summary") != dataset_manifest.get("summary"):
        errors.append("dataset_summary mismatch")

    model = artifact.get("model")
    if not isinstance(model, Mapping):
        errors.append("model must be an object")
        model = {}
    if model.get("architecture") != "xls-r-phone-event-v1":
        errors.append("model.architecture must be xls-r-phone-event-v1")
    if model.get("training_status") != "trained_uncalibrated":
        errors.append("model.training_status must bind a trained_uncalibrated report")
    if model.get("exported") is not False:
        errors.append("model.exported must remain false in offline calibration")
    for field in ("checkpoint_sha256", "training_report_sha256", "training_manifest_sha256"):
        if not _is_sha256(model.get(field)):
            errors.append(f"model.{field} must be SHA-256")
    if model.get("training_manifest_sha256") != artifact_sha256(dataset_manifest):
        errors.append("model.training_manifest_sha256 mismatch")
    summary = dataset_manifest.get("summary") if isinstance(dataset_manifest, Mapping) else {}
    if int(model.get("training_cases") or 0) < int((summary.get("split_counts") or {}).get("training") or 0):
        errors.append("model.training_cases does not cover the signed training split")
    if float(model.get("training_hours") or 0.0) < float(summary.get("training_hours") or 0.0):
        errors.append("model.training_hours does not cover the signed training split")
    if int(model.get("training_events") or 0) < int(summary.get("training_events") or 0):
        errors.append("model.training_events does not cover the signed training split")

    conformal = artifact.get("conformal")
    if not isinstance(conformal, Mapping):
        errors.append("conformal must be an object")
        conformal = {}
    for name in ("cardinality", "content", "timing"):
        _validate_conformal_block(name, conformal.get(name), errors)
        block = conformal.get(name)
        if isinstance(block, Mapping) and block.get("source_split_sha256") != split_sha256(dataset_manifest, "calibration"):
            errors.append(f"conformal.{name}.source_split_sha256 mismatch")

    temporal = artifact.get("temporal_evaluation")
    if not isinstance(temporal, Mapping):
        errors.append("temporal_evaluation must be an object")
    else:
        if int(temporal.get("cases") or 0) < DATA_REQUIREMENTS["temporal_cases_min"]:
            errors.append("temporal_evaluation.cases is inadequate")
        if int(temporal.get("catastrophic") or 0) != 0:
            errors.append("temporal_evaluation must contain zero catastrophic outcomes")
        if temporal.get("source_split_sha256") != split_sha256(dataset_manifest, "temporal"):
            errors.append("temporal_evaluation.source_split_sha256 mismatch")

    action_evidence = artifact.get("action_evidence")
    if not isinstance(action_evidence, Mapping):
        errors.append("action_evidence must be an object")
    else:
        for action, evidence in action_evidence.items():
            if action not in ACTION_GATES or not isinstance(evidence, Mapping):
                errors.append(f"action_evidence.{action} is invalid")
                continue
            result = evaluate_action_gate(
                action,
                correct=evidence.get("correct"),
                reviewed=evidence.get("reviewed"),
                catastrophic=evidence.get("catastrophic", 0),
            )
            declared = evidence.get("wilson_lower_95")
            if not _finite(declared) or abs(float(declared) - result["wilson_lower_95"]) > 1e-12:
                errors.append(f"action_evidence.{action}.wilson_lower_95 mismatch")
        if "suggestion" not in action_evidence:
            errors.append("action_evidence.suggestion is required")

    verified, reason = verify_artifact(dict(artifact), calibration_public_keys_env)
    if not verified:
        errors.append(f"calibration attestation rejected: {reason}")
    return errors


def certify_offline_prediction(
    artifact: Any,
    dataset_manifest: Any,
    *,
    action: str,
    cardinality_candidates: Sequence[int],
    content_candidates: Sequence[str],
    onset_interval: Sequence[float],
    end_interval: Sequence[float],
    calibration_public_keys_env: str = "QUALITY_V6_CALIBRATION_PUBLIC_KEYS",
    dataset_public_keys_env: str = "QUALITY_V6_DATASET_PUBLIC_KEYS",
) -> dict[str, Any]:
    """Check one selective prediction while remaining offline/review-only."""
    errors = validate_calibration_artifact(
        artifact,
        dataset_manifest,
        calibration_public_keys_env=calibration_public_keys_env,
        dataset_public_keys_env=dataset_public_keys_env,
    )
    blockers = list(errors)
    evidence = artifact.get("action_evidence", {}).get(action, {}) if isinstance(artifact, Mapping) else {}
    action_gate = evaluate_action_gate(
        action,
        correct=evidence.get("correct"),
        reviewed=evidence.get("reviewed"),
        catastrophic=evidence.get("catastrophic", 0),
    )
    if not action_gate["passed"]:
        blockers.extend(f"action:{item}" for item in action_gate["blockers"])
    if (
        len(cardinality_candidates) != 1
        or isinstance(cardinality_candidates[0], bool)
        or not isinstance(cardinality_candidates[0], int)
        or cardinality_candidates[0] < 0
    ):
        blockers.append("cardinality_conformal_set_not_singleton")
    if len(content_candidates) != 1 or not _nonempty(content_candidates[0]):
        blockers.append("content_conformal_set_not_singleton")

    def interval(name: str, values: Sequence[float], maximum_width: float) -> tuple[float, float] | None:
        if (
            len(values) != 2
            or not all(_finite(value) for value in values)
            or float(values[0]) < 0
            or float(values[1]) < float(values[0])
        ):
            blockers.append(f"{name}_interval_invalid")
            return None
        parsed = (float(values[0]), float(values[1]))
        if parsed[1] - parsed[0] > maximum_width:
            blockers.append(f"{name}_interval_too_wide")
        return parsed

    onset = interval("onset", onset_interval, MAX_ONSET_INTERVAL_WIDTH_S)
    end = interval("end", end_interval, MAX_END_INTERVAL_WIDTH_S)
    if onset and end and end[0] <= onset[1]:
        blockers.append("timing_intervals_do_not_define_positive_duration")
    return {
        "kind": "review_proposal_certification",
        "schema": PREDICTION_CERTIFICATION_SCHEMA,
        "policy_version": POLICY_VERSION,
        "eligible_offline": not blockers,
        "review_proposal_allowed": not blockers,
        # This module is intentionally incapable of granting runtime mutation.
        "automatic_apply_allowed": False,
        "runtime_authorization": False,
        "action_gate": action_gate,
        "blockers": sorted(set(blockers)),
    }


def runtime_review_proposal_authorization(certification: Mapping[str, Any] | None) -> dict[str, Any]:
    """Load hash-pinned signed evidence and authorize *review UI* only.

    This deliberately cannot authorize a mutation.  Missing files, hashes,
    signatures, singleton conformal sets or action evidence all abstain.
    """
    blockers: list[str] = []
    candidate = certification if isinstance(certification, Mapping) else {}
    if candidate.get("kind") != "review_proposal_certification":
        blockers.append("certification_kind_mismatch")
    if candidate.get("schema") != PREDICTION_CERTIFICATION_SCHEMA:
        blockers.append("certification_schema_mismatch")
    if candidate.get("policy_version") != POLICY_VERSION:
        blockers.append("certification_policy_mismatch")

    def sequence(name: str) -> list[Any]:
        value = candidate.get(name)
        if not isinstance(value, (list, tuple)) or isinstance(value, (str, bytes)):
            blockers.append(f"{name}_invalid")
            return []
        if len(value) > 32:
            blockers.append(f"{name}_too_large")
            return []
        return list(value)

    cardinality_candidates = sequence("cardinality_candidates")
    content_candidates = sequence("content_candidates")
    onset_interval = sequence("onset_interval")
    end_interval = sequence("end_interval")
    manifest_path = os.environ.get("QUALITY_V6_DATASET_MANIFEST_PATH", "").strip()
    manifest_sha = os.environ.get("QUALITY_V6_DATASET_MANIFEST_SHA256", "").strip().lower()
    artifact_path = os.environ.get("QUALITY_V6_CALIBRATION_PATH", "").strip()
    artifact_sha = os.environ.get("QUALITY_V6_CALIBRATION_SHA256", "").strip().lower()
    if os.environ.get("QUALITY_V6_PROPOSALS_ENABLED", "0").strip().lower() not in {
        "1", "true", "yes", "on",
    }:
        blockers.append("proposal_kill_switch_off")
    if not (manifest_path and artifact_path and _is_sha256(manifest_sha) and _is_sha256(artifact_sha)):
        blockers.append("pinned_artifacts_missing")
        return {
            "authorized": False, "review_only": True,
            "automatic_apply_allowed": False, "blockers": blockers,
        }
    try:
        with open(manifest_path, "rb") as handle:
            manifest_raw = handle.read()
        with open(artifact_path, "rb") as handle:
            artifact_raw = handle.read()
        if hashlib.sha256(manifest_raw).hexdigest() != manifest_sha:
            blockers.append("dataset_manifest_hash_mismatch")
        if hashlib.sha256(artifact_raw).hexdigest() != artifact_sha:
            blockers.append("calibration_hash_mismatch")
        manifest = json.loads(manifest_raw.decode("utf-8"))
        artifact = json.loads(artifact_raw.decode("utf-8"))
    except (OSError, ValueError, TypeError):
        blockers.append("artifact_load_failed")
        return {
            "authorized": False, "review_only": True,
            "automatic_apply_allowed": False, "blockers": blockers,
        }
    result = certify_offline_prediction(
        artifact, manifest, action="suggestion",
        cardinality_candidates=cardinality_candidates,
        content_candidates=content_candidates,
        onset_interval=onset_interval,
        end_interval=end_interval,
    )
    blockers.extend(result.get("blockers") or [])
    return {
        "authorized": not blockers and bool(result.get("review_proposal_allowed")),
        "review_only": True,
        "automatic_apply_allowed": False,
        "policy_version": POLICY_VERSION,
        "calibration_sha256": artifact_sha,
        "dataset_manifest_sha256": manifest_sha,
        "blockers": sorted(set(blockers)),
    }
