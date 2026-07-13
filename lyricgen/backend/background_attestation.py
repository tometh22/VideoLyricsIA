"""Cryptographic evidence for Universal background and delivery safety.

Validation metadata is useful only when it is bound to the exact bytes that
were rendered.  These helpers create and verify SHA-256 attestations without
depending on HTTP, database or provider code, so the same invariant can guard
initial renders, edits, audits and tests.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any

from background_policy import POLICY_VERSION
from content_validator import GEMINI_VALIDATOR_VERSION, LOCAL_VALIDATOR_VERSION


BACKGROUND_ATTESTATION_VERSION = "background-attestation-v1"
DELIVERY_ATTESTATION_VERSION = "universal-delivery-v1"
REQUIRED_UNIVERSAL_VALIDATORS = frozenset({
    GEMINI_VALIDATOR_VERSION,
    LOCAL_VALIDATOR_VERSION,
})


class AttestationError(RuntimeError):
    """The asset cannot be proven safe under the current Universal policy."""


def sha256_file(path: str, *, chunk_size: int = 1024 * 1024) -> str:
    if not path or not os.path.isfile(path):
        raise AttestationError(f"attestation asset is missing: {path!r}")
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validation_digest(validation: dict[str, Any]) -> str:
    """Fingerprint the safety evidence while excluding later seal fields."""
    evidence = {
        key: value
        for key, value in validation.items()
        if key not in {"attestation", "delivery_attestation"}
    }
    encoded = json.dumps(
        evidence,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _complete_primary_coverage(validation: dict[str, Any]) -> bool:
    checked = validation.get("frames_checked")
    planned = validation.get("frames_planned")
    return bool(
        isinstance(checked, int)
        and isinstance(planned, int)
        and planned > 0
        and checked == planned
        and validation.get("extraction_errors", 0) == 0
        and validation.get("check_errors", 0) == 0
    )


def _complete_secondary_coverage(validation: dict[str, Any]) -> bool:
    checked = validation.get("secondary_frames_checked")
    planned = validation.get("secondary_frames_planned")
    return bool(
        validation.get("secondary_required") is True
        and isinstance(checked, int)
        and isinstance(planned, int)
        and planned > 0
        and checked == planned
        and validation.get("secondary_extraction_errors", 0) == 0
        and validation.get("secondary_errors", 0) == 0
    )


def _assert_universal_validation(validation: dict[str, Any], policy: dict[str, Any]) -> None:
    if validation.get("passed") is not True:
        raise AttestationError("background validation did not pass")
    if policy.get("allow_people") is not False:
        raise AttestationError("Universal background unexpectedly allows people")
    if policy.get("policy_version") != POLICY_VERSION:
        raise AttestationError("runtime policy version is stale")
    if validation.get("policy_version") != POLICY_VERSION:
        raise AttestationError("validation evidence belongs to a stale policy")
    if validation.get("allow_people") is not False:
        raise AttestationError("validation evidence does not deny people")

    scope = validation.get("validation_scope")
    if scope == "deterministic_local_fallback":
        # Produced entirely by our renderer; no generated visual content exists.
        return

    validators = frozenset(validation.get("validator_stack") or [])
    if not REQUIRED_UNIVERSAL_VALIDATORS.issubset(validators):
        raise AttestationError("Universal validation is missing an independent detector")

    if scope == "each_unique_scene_clip":
        if validation.get("scene_validations_complete") is not True:
            raise AttestationError("not every unique scene has current dual-validator evidence")
        return

    if not _complete_primary_coverage(validation):
        raise AttestationError("Gemini did not inspect every planned sample")
    if not _complete_secondary_coverage(validation):
        raise AttestationError("the independent detector did not inspect every dense sample")


def build_background_attestation(
    asset_path: str,
    validation: dict[str, Any],
    policy: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(validation, dict):
        raise AttestationError("background validation evidence is missing")
    if policy.get("is_umg") is True:
        _assert_universal_validation(validation, policy)
    elif validation.get("passed") is not True:
        raise AttestationError("background validation did not pass")

    return {
        "version": BACKGROUND_ATTESTATION_VERSION,
        "asset_sha256": sha256_file(asset_path),
        "asset_size": os.path.getsize(asset_path),
        "validation_sha256": _validation_digest(validation),
        "policy_version": policy.get("policy_version"),
        "policy_mode": policy.get("policy_mode"),
        "tenant_id": policy.get("tenant_id"),
        "billing_group": policy.get("billing_group"),
        "is_universal": bool(policy.get("is_umg")),
        "allow_people": bool(policy.get("allow_people")),
        "validation_scope": validation.get("validation_scope") or "sampled_asset",
        "validator_stack": list(validation.get("validator_stack") or []),
        "sealed_at": datetime.now(timezone.utc).isoformat(),
    }


def verify_background_attestation(
    asset_path: str,
    attestation: dict[str, Any] | None,
    policy: dict[str, Any],
    *,
    validation: dict[str, Any] | None = None,
) -> None:
    if not isinstance(attestation, dict):
        raise AttestationError("background attestation is missing")
    if attestation.get("version") != BACKGROUND_ATTESTATION_VERSION:
        raise AttestationError("background attestation version is stale")
    if attestation.get("policy_version") != POLICY_VERSION:
        raise AttestationError("background attestation policy is stale")
    if policy.get("is_umg") is True:
        if attestation.get("is_universal") is not True:
            raise AttestationError("background was not sealed as Universal")
        if attestation.get("allow_people") is not False:
            raise AttestationError("background attestation allows people")
        if attestation.get("tenant_id") != policy.get("tenant_id"):
            raise AttestationError("background attestation tenant changed")
    actual_hash = sha256_file(asset_path)
    if actual_hash != attestation.get("asset_sha256"):
        raise AttestationError("rendered background bytes differ from validated bytes")
    if validation is not None:
        if _validation_digest(validation) != attestation.get("validation_sha256"):
            raise AttestationError("background validation evidence changed after sealing")


def build_delivery_attestation(
    *,
    job_dir: str,
    files: dict[str, str],
    background_attestation: dict[str, Any],
) -> dict[str, Any]:
    """Seal critical local deliverables before any upload or success state."""
    filenames = {
        "video_url": "lyric_video.mp4",
        "short_url": "short.mp4",
        "thumbnail_url": "thumbnail.jpg",
    }
    deliverables: dict[str, dict[str, Any]] = {}
    for url_key, filename in filenames.items():
        if url_key not in files:
            continue
        path = os.path.join(job_dir, filename)
        deliverables[url_key] = {
            "filename": filename,
            "sha256": sha256_file(path),
            "size": os.path.getsize(path),
        }
    required = {key for key in filenames if key in files}
    if set(deliverables) != required or not deliverables:
        raise AttestationError("not every critical deliverable could be sealed")
    return {
        "version": DELIVERY_ATTESTATION_VERSION,
        "background_sha256": background_attestation.get("asset_sha256"),
        "policy_version": background_attestation.get("policy_version"),
        "deliverables": deliverables,
        "sealed_at": datetime.now(timezone.utc).isoformat(),
    }
