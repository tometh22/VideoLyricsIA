"""Fail-closed runtime authorization for pre-editor lyric auto-repair.

The offline quality-v6 proposal certificates explicitly do not authorize
mutation. Auto-repair therefore requires its own signed, short-lived artifact
bound to the running release/config plus independent per-action switches.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from evidence_attestation import verify_artifact


SCHEMA = "lyrics-auto-repair-authorization-v1"
KIND = "lyrics_auto_repair_authorization"
_TRUE = {"1", "true", "yes", "on"}
_ACTION_GATES = {
    "timing_reversible": {"minimum_reviewed": 539, "minimum_lower_bound": 0.995},
    "content_reversible": {"minimum_reviewed": 539, "minimum_lower_bound": 0.995},
}


def _enabled(name: str) -> bool:
    return os.environ.get(name, "0").strip().lower() in _TRUE


def _authorization() -> tuple[dict[str, Any] | None, str]:
    if not _enabled("LYRIC_AUTO_REPAIR_ENABLED"):
        return None, "disabled"
    path = os.environ.get("LYRIC_AUTO_REPAIR_AUTHORIZATION_PATH", "").strip()
    expected_sha = os.environ.get(
        "LYRIC_AUTO_REPAIR_AUTHORIZATION_SHA256", "",
    ).strip().lower()
    if not path or len(expected_sha) != 64:
        return None, "authorization_missing"
    try:
        raw = Path(path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected_sha:
            return None, "authorization_hash_mismatch"
        artifact = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError):
        return None, "authorization_unreadable"
    if not isinstance(artifact, dict):
        return None, "authorization_invalid"
    verified, _reason = verify_artifact(artifact, "LYRIC_AUTO_REPAIR_PUBLIC_KEYS")
    if not verified:
        return None, "authorization_untrusted"
    if artifact.get("kind") != KIND or artifact.get("schema") != SCHEMA:
        return None, "authorization_schema_mismatch"
    if artifact.get("decision") != "GO":
        return None, "authorization_not_go"
    from transcription_quality import runtime_identity

    runtime = runtime_identity()
    if (
        artifact.get("pipeline_release") != runtime["pipeline_release"]
        or artifact.get("pipeline_config_fingerprint")
        != runtime["pipeline_config_fingerprint"]
    ):
        return None, "authorization_runtime_mismatch"
    try:
        expires = datetime.fromisoformat(
            str(artifact.get("expires_at") or "").replace("Z", "+00:00")
        )
    except ValueError:
        return None, "authorization_expiry_invalid"
    if expires.tzinfo is None or expires <= datetime.now(timezone.utc):
        return None, "authorization_expired"
    return artifact, "authorized"


def runtime_authorization() -> dict[str, Any]:
    """Return safe action grants and a bounded reason code; never raises."""
    try:
        artifact, status = _authorization()
    except Exception:
        artifact, status = None, "authorization_unavailable"
    grants: dict[str, bool] = {}
    evidence: dict[str, dict[str, Any]] = {}
    for action, gate in _ACTION_GATES.items():
        enabled_name = (
            "LYRIC_AUTO_REPAIR_TIMING_ENABLED" if action == "timing_reversible"
            else "LYRIC_AUTO_REPAIR_CONTENT_ENABLED"
        )
        record = ((artifact or {}).get("actions") or {}).get(action)
        reviewed = record.get("reviewed") if isinstance(record, dict) else 0
        lower_bound = record.get("precision_lower_95") if isinstance(record, dict) else 0
        catastrophic = record.get("catastrophic") if isinstance(record, dict) else None
        independent_families = (
            record.get("minimum_independent_source_families")
            if isinstance(record, dict) else 0
        )
        record_valid = bool(
            isinstance(record, dict)
            and record.get("enabled") is True
            and isinstance(reviewed, int) and not isinstance(reviewed, bool)
            and reviewed >= gate["minimum_reviewed"]
            and isinstance(lower_bound, (int, float))
            and not isinstance(lower_bound, bool)
            and lower_bound >= gate["minimum_lower_bound"]
            and catastrophic == 0
            and isinstance(independent_families, int)
            and independent_families >= 2
        )
        grants[action] = bool(
            artifact and record_valid and _enabled(enabled_name)
        )
        if record_valid:
            evidence[action] = {
                "reviewed": reviewed,
                "precision_lower_95": float(lower_bound),
                "catastrophic": 0,
                "minimum_independent_source_families": independent_families,
            }
    return {
        "schema": SCHEMA,
        "status": status,
        "actions": grants,
        "evidence": evidence,
        "authorization_sha256": (
            os.environ.get("LYRIC_AUTO_REPAIR_AUTHORIZATION_SHA256", "")[:64]
            if artifact else None
        ),
    }


def candidate_has_independent_evidence(candidate: Any, action: str) -> bool:
    """Require independent source families on every individual candidate."""
    if not isinstance(candidate, dict):
        return False
    families = candidate.get("source_families")
    if not isinstance(families, (list, tuple, set)):
        evidence = candidate.get("evidence")
        if isinstance(evidence, dict):
            families = evidence.get("source_families")
    if not isinstance(families, (list, tuple, set)):
        return False
    distinct = {
        str(value).strip().lower() for value in families
        if isinstance(value, str) and value.strip()
    }
    # Varying stems/mixes, slowed views, or multiple calls to the same model
    # stay one family. The artifact and this candidate must both show two.
    return len(distinct) >= 2 and action in _ACTION_GATES
