"""Runtime registration for a verified LoRA hypothesis family.

The runtime intentionally accepts metadata and optional words only after a
research evaluation report marks the adapter as an additional family.  It
cannot replace the base Whisper family and it never creates text by itself.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any


def _enabled() -> bool:
    return os.environ.get("LORA_V1_FAMILY_ENABLED", "0").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_verified_family(report_path: str | os.PathLike[str] | None = None) -> dict[str, Any] | None:
    """Return an attested family descriptor or ``None`` (fail closed)."""
    if not _enabled():
        return None
    path = Path(report_path or os.environ.get("LORA_V1_EVAL_REPORT", "").strip())
    if not path.is_file():
        return None
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    gate = (
        report.get("replacement_gate")
        or report.get("runtime_policy")
        or report.get("gate")
        or {}
    )
    if gate.get("additional_family_only") is not True:
        return None
    if gate.get("runtime_replacement_allowed") is True:
        return None
    # A one-step smoke adapter, or an adapter that merely has files on disk,
    # is never eligible.  Promotion requires a completed held-out inference
    # replay and an explicit evaluation gate; this is what prevents the LoRA
    # family from entering consensus before it has earned that role.
    if report.get("pipeline_validated") is not True:
        return None
    evaluation_passed = report.get("evaluation_passed") is True or (
        isinstance(report.get("gate"), dict) and report["gate"].get("passed") is True
    )
    if not evaluation_passed:
        return None
    artifact = report.get("adapter_path") or (report.get("candidate_artifact") or {}).get("path")
    if not artifact or not Path(artifact).exists():
        return None
    expected = report.get("adapter_sha256") or (report.get("candidate_artifact") or {}).get("sha256")
    if expected:
        candidate = Path(artifact)
        if candidate.is_file():
            actual = _sha256(candidate)
        elif (candidate / "adapter_model.safetensors").is_file():
            actual = _sha256(candidate / "adapter_model.safetensors")
        else:
            return None
        if actual != str(expected):
            return None
    return {
        "name": "lora_v1",
        "family": "openai_whisper_large_v3_turbo_lora_v1",
        "role": "additional_consensus_family",
        "artifact": str(artifact),
        "evaluation_report": str(path),
        "replacement_allowed": False,
    }


def attach_hypothesis(result: dict[str, Any], words: list[dict[str, Any]], *, report_path: str | None = None) -> bool:
    """Attach LoRA words to a result only when the family is verified."""
    family = load_verified_family(report_path)
    if family is None or not isinstance(result, dict) or not isinstance(words, list):
        return False
    result["_lora_asr_words"] = [dict(item) for item in words if isinstance(item, dict)]
    result["_lora_asr_family"] = family["family"]
    result["_lora_family_role"] = family["role"]
    return True
