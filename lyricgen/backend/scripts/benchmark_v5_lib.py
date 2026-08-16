#!/usr/bin/env python3
"""Strict validation and scoring primitives for transcription benchmark v5.

Metric implementations use only the standard library. Evidence receipts use
the repository-pinned ``cryptography`` package for Ed25519 verification.
"""
from __future__ import annotations

import hashlib
import json
import math
import statistics
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable, Sequence

from evidence_attestation import lyric_snapshot_hash, sign_artifact, verify_artifact


SCHEMA_VERSION = 5
SYSTEMS = ("current", "candidate", "rotor")
SPLITS = ("dev", "holdout")
EVENT_TYPES = ("lexical", "vocalization", "mixed")
BOUNDARY_TOLERANCES_S = (0.1, 0.2)
RELEASE_TARGETS = {
    "cases": 50,
    "dev_cases": 30,
    "holdout_cases": 20,
    "live_cases": 20,
    "studio_cases": 20,
    "adversarial_cases": 10,
    "repetition_or_adlib_cases": 10,
    "crowd_or_chorus_cases": 5,
    "event_count_f1": .95,
    "vocalization_recall": .90,
    "onset_p90_s": .50,
    "end_p90_s": .75,
    "wer_regression_absolute": .01,
    "automatic_precision": .99,
    "automatic_coverage": .60,
    "shadow_decisions": 400,
    "shadow_days": 30,
    "operator_p50_minutes": 5.0,
    "operator_p90_minutes": 10.0,
}


class BenchmarkValidationError(ValueError):
    """Raised when a benchmark is not safe to score."""

    def __init__(self, errors: Sequence[str]):
        self.errors = list(errors)
        super().__init__("benchmark validation failed: " + "; ".join(self.errors))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"invalid JSON {path}: {exc}") from exc


def _is_sha256(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    return all(character in "0123456789abcdef" for character in value)


def _is_runtime_fingerprint(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 16
        and all(character in "0123456789abcdef" for character in value)
    )


def _resolve_artifact(root: Path, raw_path: Any) -> Path:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("artifact path must be a non-empty relative string")
    candidate = Path(raw_path)
    if candidate.is_absolute():
        raise ValueError(f"absolute artifact path is forbidden: {raw_path}")
    resolved_root = root.resolve()
    resolved = (root / candidate).resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ValueError(f"artifact path escapes manifest directory: {raw_path}")
    return resolved


def _artifact(
    root: Path,
    descriptor: Any,
    label: str,
    errors: list[str],
) -> tuple[Path | None, Any]:
    if not isinstance(descriptor, dict):
        errors.append(f"{label}: artifact descriptor must be an object")
        return None, None
    expected_hash = descriptor.get("sha256")
    if not _is_sha256(expected_hash):
        errors.append(f"{label}: sha256 must be 64 lowercase hexadecimal characters")
        return None, None
    try:
        path = _resolve_artifact(root, descriptor.get("path"))
    except ValueError as exc:
        errors.append(f"{label}: {exc}")
        return None, None
    if not path.is_file():
        errors.append(f"{label}: missing artifact {path}")
        return path, None
    actual_hash = sha256_file(path)
    if actual_hash != expected_hash:
        errors.append(
            f"{label}: hash mismatch expected={expected_hash} actual={actual_hash}"
        )
        return path, None
    try:
        return path, read_json(path)
    except ValueError as exc:
        errors.append(f"{label}: {exc}")
        return path, None


def _binary_artifact(
    root: Path,
    descriptor: Any,
    label: str,
    errors: list[str],
) -> Path | None:
    if not isinstance(descriptor, dict):
        errors.append(f"{label}: artifact descriptor must be an object")
        return None
    expected_hash = descriptor.get("sha256")
    if not _is_sha256(expected_hash):
        errors.append(f"{label}: sha256 must be 64 lowercase hexadecimal characters")
        return None
    try:
        path = _resolve_artifact(root, descriptor.get("path"))
    except ValueError as exc:
        errors.append(f"{label}: {exc}")
        return None
    if not path.is_file():
        errors.append(f"{label}: missing artifact {path}")
    elif sha256_file(path) != expected_hash:
        errors.append(f"{label}: hash mismatch")
    return path


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _validate_segments(segments: Any, label: str, errors: list[str]) -> None:
    if not isinstance(segments, list) or not segments:
        errors.append(f"{label}: segments must be a non-empty list")
        return
    previous_start = -1.0
    previous_end = -1.0
    for index, segment in enumerate(segments):
        item_label = f"{label}.segments[{index}]"
        if not isinstance(segment, dict):
            errors.append(f"{item_label}: must be an object")
            continue
        start = segment.get("start")
        end = segment.get("end")
        if not _finite_number(start) or not _finite_number(end):
            errors.append(f"{item_label}: start/end must be finite numbers")
            continue
        start = float(start)
        end = float(end)
        if start < 0 or end <= start:
            errors.append(f"{item_label}: require 0 <= start < end")
        if start <= previous_start:
            errors.append(f"{item_label}: starts must be strictly increasing")
        if start < previous_end - 1e-6:
            errors.append(f"{item_label}: destructive overlap with previous event")
        previous_start, previous_end = start, end
        if not isinstance(segment.get("text"), str):
            errors.append(f"{item_label}: text must be a string")
        if segment.get("event_type") not in EVENT_TYPES:
            errors.append(
                f"{item_label}: event_type must be one of {', '.join(EVENT_TYPES)}"
            )


def _same_json(left: Any, right: Any) -> bool:
    return json.dumps(left, ensure_ascii=False, sort_keys=True, separators=(",", ":")) == json.dumps(
        right,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _require_attestation(artifact: Any, env_name: str, label: str,
                         errors: list[str]) -> bool:
    verified, reason = verify_artifact(artifact, env_name)
    if not verified:
        errors.append(f"{label}: unauthenticated evidence ({reason})")
    return verified


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def shadow_ledger_attestation(ledger: dict[str, Any], private_key_b64: str,
                              key_id: str) -> dict[str, str]:
    """Return the server-side receipt for an immutable shadow ledger.

    This helper is intentionally deterministic so the trusted exporter and
    the offline gate use exactly the same bytes. The private key belongs only
    in the trusted exporter; CI receives an allow-listed public key.
    """
    return sign_artifact(ledger, private_key_b64, key_id)["attestation"]


def _parse_utc_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _validate_shadow_ledger(ledger: Any, label: str,
                            errors: list[str]) -> None:
    if not isinstance(ledger, dict):
        errors.append(f"{label}: JSON root must be an object")
        return
    if ledger.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"{label}.schema_version must be {SCHEMA_VERSION}")
    if not _nonempty_string(ledger.get("candidate_release")):
        errors.append(f"{label}.candidate_release must be non-empty")
    if not _is_sha256(ledger.get("candidate_config_sha256")):
        errors.append(f"{label}.candidate_config_sha256 must be SHA-256")
    if not _is_runtime_fingerprint(ledger.get("pipeline_config_fingerprint")):
        errors.append(f"{label}.pipeline_config_fingerprint must be 16-hex")
    rows = ledger.get("decisions")
    if not isinstance(rows, list):
        errors.append(f"{label}.decisions must be a list")
        return
    seen: set[str] = set()
    for index, row in enumerate(rows):
        prefix = f"{label}.decisions[{index}]"
        if not isinstance(row, dict):
            errors.append(f"{prefix}: must be an object")
            continue
        decision_id = row.get("decision_id")
        if not _is_sha256(decision_id):
            errors.append(f"{prefix}.decision_id must be SHA-256")
        elif decision_id in seen:
            errors.append(f"{prefix}: duplicate decision_id {decision_id}")
        else:
            seen.add(decision_id)
        if _parse_utc_timestamp(row.get("occurred_at")) is None:
            errors.append(f"{prefix}.occurred_at must be timezone-aware ISO-8601")
        for field in ("eligible", "would_approve", "reviewed"):
            if not isinstance(row.get(field), bool):
                errors.append(f"{prefix}.{field} must be boolean")
        if row.get("eligible") is False and row.get("would_approve") is True:
            errors.append(f"{prefix}: ineligible decision cannot approve")
        if row.get("would_approve") is True and row.get("reviewed") is True:
            for field in ("correct", "catastrophic"):
                if not isinstance(row.get(field), bool):
                    errors.append(
                        f"{prefix}.{field} must be boolean for reviewed approvals"
                    )
            if row.get("correct") is True and row.get("catastrophic") is True:
                errors.append(f"{prefix}: approval cannot be correct and catastrophic")
        for field in (
            "candidate_release", "candidate_config_sha256",
            "pipeline_config_fingerprint",
        ):
            if row.get(field) != ledger.get(field):
                errors.append(f"{prefix}.{field} must match ledger identity")
    receipt = ledger.get("attestation")
    if not isinstance(receipt, dict):
        errors.append(f"{label}.attestation must be an object")
    else:
        if receipt.get("algorithm") != "Ed25519":
            errors.append(f"{label}.attestation.algorithm must be Ed25519")
        if not _nonempty_string(receipt.get("key_id")):
            errors.append(f"{label}.attestation.key_id must be non-empty")
        if not _is_sha256(receipt.get("payload_sha256")):
            errors.append(f"{label}.attestation.payload_sha256 must be SHA-256")
        if not _nonempty_string(receipt.get("signature")):
            errors.append(f"{label}.attestation.signature must be non-empty")


def _normalized_identity(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    return " ".join(normalized.split())


def validate_manifest(manifest_path: Path) -> list[str]:
    """Return every validation error; an empty list means safe to score."""
    manifest_path = Path(manifest_path)
    if not manifest_path.is_file():
        return [f"missing manifest: {manifest_path}"]
    try:
        manifest = read_json(manifest_path)
    except ValueError as exc:
        return [str(exc)]
    if not isinstance(manifest, dict):
        return ["manifest root must be an object"]

    errors: list[str] = []
    root = manifest_path.parent
    if manifest.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    if not _nonempty_string(manifest.get("benchmark_id")):
        errors.append("benchmark_id must be a non-empty string")

    systems = manifest.get("systems")
    system_pins: dict[str, tuple[str, str]] = {}
    system_fingerprints: dict[str, str] = {}
    if not isinstance(systems, dict) or set(systems) != set(SYSTEMS):
        errors.append(f"systems must contain exactly: {', '.join(SYSTEMS)}")
        systems = {}
    for system_name in SYSTEMS:
        spec = systems.get(system_name)
        label = f"systems.{system_name}"
        if not isinstance(spec, dict):
            errors.append(f"{label}: must be an object")
            continue
        release = spec.get("release")
        if not _nonempty_string(release):
            errors.append(f"{label}.release must be non-empty")
        if spec.get("render") is not False:
            errors.append(f"{label}.render must be exactly false")
        config_path, config = _artifact(root, spec.get("config"), f"{label}.config", errors)
        config_hash = (spec.get("config") or {}).get("sha256") if isinstance(spec.get("config"), dict) else None
        if config_path is not None and not isinstance(config, dict):
            errors.append(f"{label}.config: JSON root must be an object")
        if _nonempty_string(release) and _is_sha256(config_hash):
            system_pins[system_name] = (release, config_hash)
        if isinstance(config, dict) and _is_runtime_fingerprint(
            config.get("pipeline_config_fingerprint")
        ):
            system_fingerprints[system_name] = config["pipeline_config_fingerprint"]

    shadow = manifest.get("shadow_evaluation")
    if shadow is not None:
        if not isinstance(shadow, dict):
            errors.append("shadow_evaluation must be an object")
        else:
            _path, ledger = _artifact(
                root, shadow.get("ledger"), "shadow_evaluation.ledger", errors,
            )
            if ledger is not None:
                _validate_shadow_ledger(
                    ledger, "shadow_evaluation.ledger", errors,
                )

    entries = manifest.get("entries")
    if not isinstance(entries, list) or not entries:
        errors.append("entries must be a non-empty list")
        return errors

    seen_cases: set[str] = set()
    seen_operator_event_ids: set[str] = set()
    seen_operator_jobs: set[tuple[str, str]] = set()
    seen_cost_requests: set[tuple[str, str]] = set()
    leakage: dict[str, dict[str, str]] = {
        "artist": {},
        "song": {},
        "master": {},
        "audio_sha256": {},
    }
    for entry_index, entry in enumerate(entries):
        prefix = f"entries[{entry_index}]"
        if not isinstance(entry, dict):
            errors.append(f"{prefix}: must be an object")
            continue
        case_id = entry.get("case_id")
        if not _nonempty_string(case_id):
            errors.append(f"{prefix}.case_id must be non-empty")
            case_id = f"<invalid-{entry_index}>"
        elif case_id in seen_cases:
            errors.append(f"{prefix}: duplicate case_id {case_id}")
        else:
            seen_cases.add(case_id)
        split = entry.get("split")
        if split not in SPLITS:
            errors.append(f"{prefix}.split must be dev or holdout")

        identity = entry.get("identity")
        if not isinstance(identity, dict):
            errors.append(f"{prefix}.identity must be an object")
            identity = {}
        for field in ("artist", "song", "master", "audio_sha256"):
            value = identity.get(field)
            valid = _is_sha256(value) if field == "audio_sha256" else _nonempty_string(value)
            if not valid:
                errors.append(f"{prefix}.identity.{field} is required")
                continue
            normalized = value if field == "audio_sha256" else _normalized_identity(value)
            prior_split = leakage[field].get(normalized)
            if split in SPLITS and prior_split is not None and prior_split != split:
                errors.append(
                    f"{prefix}: {field} leakage across {prior_split}/{split}: {value}"
                )
            elif split in SPLITS:
                leakage[field][normalized] = split

        audio_descriptor = entry.get("audio")
        _binary_artifact(root, audio_descriptor, f"{prefix}.audio", errors)
        audio_hash = audio_descriptor.get("sha256") if isinstance(audio_descriptor, dict) else None
        if identity.get("audio_sha256") != audio_hash:
            errors.append(f"{prefix}: identity.audio_sha256 must equal audio.sha256")

        annotations = entry.get("annotations")
        if not isinstance(annotations, list) or len(annotations) != 2:
            errors.append(f"{prefix}.annotations must contain exactly two annotators")
            annotations = annotations if isinstance(annotations, list) else []
        annotator_ids: list[str] = []
        annotation_hashes: list[str] = []
        for annotation_index, descriptor in enumerate(annotations):
            label = f"{prefix}.annotations[{annotation_index}]"
            _path, bundle = _artifact(root, descriptor, label, errors)
            descriptor_id = descriptor.get("annotator_id") if isinstance(descriptor, dict) else None
            if not _nonempty_string(descriptor_id):
                errors.append(f"{label}.annotator_id must be non-empty")
            else:
                annotator_ids.append(descriptor_id)
            descriptor_hash = descriptor.get("sha256") if isinstance(descriptor, dict) else None
            if _is_sha256(descriptor_hash):
                annotation_hashes.append(descriptor_hash)
            if isinstance(bundle, dict):
                _require_attestation(
                    bundle, "BENCHMARK_ANNOTATION_PUBLIC_KEYS", label, errors,
                )
                if bundle.get("schema_version") != SCHEMA_VERSION:
                    errors.append(f"{label}: schema_version must be {SCHEMA_VERSION}")
                if bundle.get("case_id") != case_id:
                    errors.append(f"{label}: case_id mismatch")
                if bundle.get("annotator_id") != descriptor_id:
                    errors.append(f"{label}: annotator_id mismatch")
                if bundle.get("signer_id") != descriptor_id:
                    errors.append(f"{label}: signer_id must match annotator_id")
                if bundle.get("source") != "authenticated_annotation_service_v1":
                    errors.append(f"{label}: source must be authenticated")
                _validate_segments(bundle.get("segments"), label, errors)
        normalized_annotator_ids = {
            _normalized_identity(annotator_id) for annotator_id in annotator_ids
        }
        if len(annotator_ids) == 2 and len(normalized_annotator_ids) != 2:
            errors.append(f"{prefix}: annotators must be distinct")

        adjudication_descriptor = entry.get("adjudication")
        _path, adjudication = _artifact(
            root, adjudication_descriptor, f"{prefix}.adjudication", errors
        )
        adjudicator_id = (
            adjudication_descriptor.get("adjudicator_id")
            if isinstance(adjudication_descriptor, dict)
            else None
        )
        if not _nonempty_string(adjudicator_id):
            errors.append(f"{prefix}.adjudication.adjudicator_id must be non-empty")
        elif _normalized_identity(adjudicator_id) in normalized_annotator_ids:
            errors.append(f"{prefix}: adjudicator must differ from both annotators")
        adjudication_hash = (
            adjudication_descriptor.get("sha256")
            if isinstance(adjudication_descriptor, dict)
            else None
        )
        if isinstance(adjudication, dict):
            _require_attestation(
                adjudication, "BENCHMARK_ANNOTATION_PUBLIC_KEYS",
                f"{prefix}.adjudication", errors,
            )
            if adjudication.get("schema_version") != SCHEMA_VERSION:
                errors.append(f"{prefix}.adjudication: schema_version must be {SCHEMA_VERSION}")
            if adjudication.get("case_id") != case_id:
                errors.append(f"{prefix}.adjudication: case_id mismatch")
            if adjudication.get("adjudicator_id") != adjudicator_id:
                errors.append(f"{prefix}.adjudication: adjudicator_id mismatch")
            if adjudication.get("signer_id") != adjudicator_id:
                errors.append(f"{prefix}.adjudication: signer_id mismatch")
            if adjudication.get("source") != "authenticated_annotation_service_v1":
                errors.append(f"{prefix}.adjudication: source must be authenticated")
            source_hashes = adjudication.get("source_annotation_sha256")
            if not isinstance(source_hashes, list) or sorted(source_hashes) != sorted(annotation_hashes):
                errors.append(
                    f"{prefix}.adjudication: source hashes must match exactly both annotations"
                )
            _validate_segments(adjudication.get("segments"), f"{prefix}.adjudication", errors)

        gold_descriptor = entry.get("gold")
        _path, gold = _artifact(root, gold_descriptor, f"{prefix}.gold", errors)
        if not isinstance(gold_descriptor, dict) or gold_descriptor.get("verified") is not True:
            errors.append(f"{prefix}.gold.verified must be exactly true")
        if isinstance(gold, dict):
            _require_attestation(
                gold, "BENCHMARK_ANNOTATION_PUBLIC_KEYS",
                f"{prefix}.gold", errors,
            )
            if gold.get("schema_version") != SCHEMA_VERSION:
                errors.append(f"{prefix}.gold: schema_version must be {SCHEMA_VERSION}")
            if gold.get("case_id") != case_id:
                errors.append(f"{prefix}.gold: case_id mismatch")
            if gold.get("verified") is not True:
                errors.append(f"{prefix}.gold: file must declare verified=true")
            if gold.get("verified_by") != adjudicator_id:
                errors.append(f"{prefix}.gold: verified_by must be the adjudicator")
            if gold.get("signer_id") != adjudicator_id:
                errors.append(f"{prefix}.gold: signer_id mismatch")
            if gold.get("source") != "authenticated_annotation_service_v1":
                errors.append(f"{prefix}.gold: source must be authenticated")
            if gold.get("adjudication_sha256") != adjudication_hash:
                errors.append(f"{prefix}.gold: adjudication hash mismatch")
            _validate_segments(gold.get("segments"), f"{prefix}.gold", errors)
            if isinstance(adjudication, dict) and not _same_json(
                gold.get("segments"), adjudication.get("segments")
            ):
                errors.append(f"{prefix}.gold: segments must exactly equal adjudication")

        outputs = entry.get("outputs")
        if not isinstance(outputs, dict) or set(outputs) != set(SYSTEMS):
            errors.append(f"{prefix}.outputs must contain exactly: {', '.join(SYSTEMS)}")
            outputs = {}
        for system_name in SYSTEMS:
            descriptor = outputs.get(system_name)
            label = f"{prefix}.outputs.{system_name}"
            _path, output = _artifact(root, descriptor, label, errors)
            if not isinstance(output, dict):
                continue
            pin = system_pins.get(system_name)
            if output.get("schema_version") != SCHEMA_VERSION:
                errors.append(f"{label}: schema_version must be {SCHEMA_VERSION}")
            if output.get("case_id") != case_id:
                errors.append(f"{label}: case_id mismatch")
            if output.get("system") != system_name:
                errors.append(f"{label}: system mismatch")
            if pin is not None and (
                output.get("release") != pin[0] or output.get("config_sha256") != pin[1]
            ):
                errors.append(f"{label}: release/config does not match system pin")
            if output.get("render") is not False:
                errors.append(f"{label}: render must be exactly false")
            if "operator_review_minutes" not in output:
                errors.append(f"{label}: operator_review_minutes must be present (number or null)")
            elif output["operator_review_minutes"] is not None and (
                not _finite_number(output["operator_review_minutes"])
                or output["operator_review_minutes"] < 0
            ):
                errors.append(f"{label}: operator_review_minutes must be non-negative or null")
            operator_verified = output.get("operator_review_minutes") is None
            if output.get("operator_review_minutes") is not None:
                _evidence_path, operator_evidence = _artifact(
                    root, output.get("operator_evidence"),
                    f"{label}.operator_evidence", errors,
                )
                operator_verified = bool(
                    isinstance(operator_evidence, dict)
                    and _require_attestation(
                        operator_evidence,
                        "BENCHMARK_OPERATOR_EVIDENCE_PUBLIC_KEYS",
                        f"{label}.operator_evidence", errors,
                    )
                    and operator_evidence.get("schema")
                    == "server-editor-session-evidence-v1"
                    and operator_evidence.get("case_id") == case_id
                    and operator_evidence.get("system") == system_name
                    and operator_evidence.get("source")
                    == "server_product_events_v1"
                    and operator_evidence.get("active_minutes")
                    == output.get("operator_review_minutes")
                    and isinstance(operator_evidence.get("event_ids"), list)
                    and bool(operator_evidence.get("event_ids"))
                    and all(
                        _nonempty_string(event_id)
                        for event_id in operator_evidence.get("event_ids")
                    )
                    and len(set(operator_evidence.get("event_ids")))
                    == len(operator_evidence.get("event_ids"))
                    and _nonempty_string(operator_evidence.get("job_id"))
                    and isinstance(operator_evidence.get("revision"), int)
                    and not isinstance(operator_evidence.get("revision"), bool)
                    and operator_evidence.get("revision") >= 0
                    and _is_sha256(operator_evidence.get("snapshot_sha256"))
                    and _nonempty_string(operator_evidence.get("operator_id"))
                    and operator_evidence.get("pipeline_release")
                    == output.get("release")
                    and operator_evidence.get("config_sha256")
                    == output.get("config_sha256")
                    and _is_runtime_fingerprint(
                        operator_evidence.get("pipeline_config_fingerprint")
                    )
                    and operator_evidence.get("pipeline_config_fingerprint")
                    == system_fingerprints.get(system_name)
                    and operator_evidence.get("snapshot_sha256")
                    == lyric_snapshot_hash(output.get("segments"))
                    and operator_evidence.get("scored_segments_sha256")
                    == lyric_snapshot_hash(
                        output.get("segments"), include_event_type=True,
                    )
                )
                if not operator_verified:
                    errors.append(f"{label}.operator_evidence is not server-derived")
                else:
                    operator_job = (system_name, str(operator_evidence["job_id"]))
                    if operator_job in seen_operator_jobs:
                        errors.append(f"{label}.operator_evidence reuses a job")
                    seen_operator_jobs.add(operator_job)
                    for event_id in operator_evidence["event_ids"]:
                        if event_id in seen_operator_event_ids:
                            errors.append(
                                f"{label}.operator_evidence reuses event_id {event_id}"
                            )
                        seen_operator_event_ids.add(event_id)
            if "cost_usd" not in output:
                errors.append(f"{label}: cost_usd must be present (number or null)")
            elif output["cost_usd"] is not None and (
                not _finite_number(output["cost_usd"]) or output["cost_usd"] < 0
            ):
                errors.append(f"{label}: cost_usd must be non-negative or null")
            cost_verified = output.get("cost_usd") is None
            if output.get("cost_usd") is not None:
                _evidence_path, cost_evidence = _artifact(
                    root, output.get("cost_evidence"),
                    f"{label}.cost_evidence", errors,
                )
                line_items = (
                    cost_evidence.get("line_items")
                    if isinstance(cost_evidence, dict) else None
                )
                source_artifacts_valid = True
                source_receipts: dict[tuple[str, str], dict] = {}
                sources = (
                    cost_evidence.get("source_artifacts")
                    if isinstance(cost_evidence, dict) else None
                )
                if not isinstance(sources, list) or not sources:
                    source_artifacts_valid = False
                else:
                    for source_index, source in enumerate(sources):
                        _source_path, source_payload = _artifact(
                            root, source,
                            f"{label}.cost_evidence.source_artifacts[{source_index}]",
                            errors,
                        )
                        if not isinstance(source_payload, dict) or (
                            source_payload.get("schema")
                            != "provider-billing-receipts-v1"
                            or source_payload.get("currency") != "USD"
                            or source_payload.get("billing_period")
                            != cost_evidence.get("billing_period")
                            or source_payload.get("invoice_snapshot_id")
                            != cost_evidence.get("invoice_snapshot_id")
                        ):
                            source_artifacts_valid = False
                            continue
                        for receipt in source_payload.get("receipts") or []:
                            if not isinstance(receipt, dict):
                                source_artifacts_valid = False
                                continue
                            receipt_key = (
                                str(receipt.get("provider") or ""),
                                str(receipt.get("request_id") or ""),
                            )
                            if not all(receipt_key) or receipt_key in source_receipts:
                                source_artifacts_valid = False
                            source_receipts[receipt_key] = receipt
                cost_verified = bool(
                    isinstance(cost_evidence, dict)
                    and _require_attestation(
                        cost_evidence, "BENCHMARK_FINOPS_PUBLIC_KEYS",
                        f"{label}.cost_evidence", errors,
                    )
                    and cost_evidence.get("schema") == "reconciled-cost-ledger-v1"
                    and cost_evidence.get("case_id") == case_id
                    and cost_evidence.get("system") == system_name
                    and cost_evidence.get("reconciled") is True
                    and cost_evidence.get("cost_complete") is True
                    and cost_evidence.get("total_usd") == output.get("cost_usd")
                    and cost_evidence.get("currency") == "USD"
                    and _nonempty_string(cost_evidence.get("pricing_version"))
                    and _nonempty_string(cost_evidence.get("billing_period"))
                    and _nonempty_string(cost_evidence.get("invoice_snapshot_id"))
                    and _nonempty_string(cost_evidence.get("reconciliation_id"))
                    and _nonempty_string(cost_evidence.get("reconciled_by"))
                    and _parse_utc_timestamp(cost_evidence.get("reconciled_at"))
                    is not None
                    and _parse_utc_timestamp(cost_evidence.get("reconciled_at"))
                    <= datetime.now(timezone.utc)
                    and cost_evidence.get("release") == output.get("release")
                    and cost_evidence.get("config_sha256") == output.get("config_sha256")
                    and _is_sha256(cost_evidence.get("audio_sha256"))
                    and cost_evidence.get("audio_sha256")
                    == identity.get("audio_sha256")
                    and isinstance(cost_evidence.get("source_artifacts"), list)
                    and bool(cost_evidence.get("source_artifacts"))
                    and source_artifacts_valid
                    and all(
                        isinstance(source, dict)
                        and _nonempty_string(source.get("source"))
                        and _is_sha256(source.get("sha256"))
                        for source in cost_evidence.get("source_artifacts")
                    )
                    and isinstance(line_items, list) and bool(line_items)
                    and all(
                        isinstance(item, dict)
                        and _nonempty_string(item.get("provider"))
                        and _nonempty_string(item.get("sku"))
                        and _nonempty_string(item.get("request_id"))
                        and _nonempty_string(item.get("unit_type"))
                        and item.get("currency") == "USD"
                        and _nonempty_string(item.get("source_receipt_sha256"))
                        and _is_sha256(item.get("source_receipt_sha256"))
                        and _finite_number(item.get("units"))
                        and float(item.get("units")) >= 0
                        and _finite_number(item.get("cost_usd"))
                        and float(item.get("cost_usd")) >= 0
                        for item in line_items
                    )
                    and all(
                        (
                            source_receipts.get((
                                str(item["provider"]), str(item["request_id"]),
                            ), {}).get("sku") == item.get("sku")
                            and source_receipts.get((
                                str(item["provider"]), str(item["request_id"]),
                            ), {}).get("unit_type") == item.get("unit_type")
                            and source_receipts.get((
                                str(item["provider"]), str(item["request_id"]),
                            ), {}).get("currency") == item.get("currency")
                            and _finite_number(source_receipts.get((
                                str(item["provider"]), str(item["request_id"]),
                            ), {}).get("units"))
                            and abs(float(source_receipts.get((
                                str(item["provider"]), str(item["request_id"]),
                            ), {})["units"]) - float(item["units"])) <= 1e-8
                            and _finite_number(source_receipts.get((
                                str(item["provider"]), str(item["request_id"]),
                            ), {}).get("cost_usd"))
                            and abs(float(source_receipts.get((
                                str(item["provider"]), str(item["request_id"]),
                            ), {})["cost_usd"]) - float(item["cost_usd"])) <= 1e-8
                        )
                        for item in line_items
                    )
                    and len({
                        (str(item["provider"]), str(item["request_id"]))
                        for item in line_items
                    }) == len(line_items)
                    and abs(
                        sum(float(item["cost_usd"]) for item in line_items)
                        - float(output.get("cost_usd"))
                    ) <= 1e-8
                )
                if not cost_verified:
                    errors.append(f"{label}.cost_evidence is incomplete or unreconciled")
                else:
                    for item in line_items:
                        request_identity = (
                            str(item["provider"]), str(item["request_id"]),
                        )
                        if request_identity in seen_cost_requests:
                            errors.append(
                                f"{label}.cost_evidence reuses provider request "
                                f"{request_identity[0]}:{request_identity[1]}"
                            )
                        seen_cost_requests.add(request_identity)
            _validate_segments(output.get("segments"), label, errors)
    return errors


def load_validated_manifest(manifest_path: Path) -> dict[str, Any]:
    errors = validate_manifest(manifest_path)
    if errors:
        raise BenchmarkValidationError(errors)
    manifest = read_json(Path(manifest_path))
    assert isinstance(manifest, dict)
    return manifest


def _normalise_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text or "").casefold()
    words: list[str] = []
    current: list[str] = []
    for character in normalized:
        if character.isalnum():
            current.append(character)
        elif current:
            words.append("".join(current))
            current = []
    if current:
        words.append("".join(current))
    return " ".join(words)


def _levenshtein(reference: Sequence[Any], hypothesis: Sequence[Any]) -> int:
    if len(reference) < len(hypothesis):
        reference, hypothesis = hypothesis, reference
    previous = list(range(len(hypothesis) + 1))
    for row_index, ref_item in enumerate(reference, start=1):
        current = [row_index]
        for column_index, hyp_item in enumerate(hypothesis, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[column_index] + 1,
                    previous[column_index - 1] + (ref_item != hyp_item),
                )
            )
        previous = current
    return previous[-1]


def error_rates(reference_text: str, hypothesis_text: str) -> tuple[float, float]:
    reference = _normalise_text(reference_text)
    hypothesis = _normalise_text(hypothesis_text)
    ref_words, hyp_words = reference.split(), hypothesis.split()
    wer = _levenshtein(ref_words, hyp_words) / max(1, len(ref_words))
    ref_chars = list(reference.replace(" ", ""))
    hyp_chars = list(hypothesis.replace(" ", ""))
    cer = _levenshtein(ref_chars, hyp_chars) / max(1, len(ref_chars))
    return wer, cer


def _segment_text(segments: Sequence[dict[str, Any]]) -> str:
    return " ".join(str(segment.get("text") or "") for segment in segments)


def _intersection_over_union(left: dict[str, Any], right: dict[str, Any]) -> float:
    intersection = max(0.0, min(float(left["end"]), float(right["end"])) - max(float(left["start"]), float(right["start"])))
    union = max(float(left["end"]), float(right["end"])) - min(float(left["start"]), float(right["start"]))
    return intersection / union if union > 0 else 0.0


def _text_similarity(left: str, right: str) -> float:
    left_normalized = _normalise_text(left)
    right_normalized = _normalise_text(right)
    if not left_normalized or not right_normalized:
        return 0.0
    return SequenceMatcher(None, left_normalized, right_normalized).ratio()


def monotonic_alignment(
    ground: Sequence[dict[str, Any]],
    hypothesis: Sequence[dict[str, Any]],
) -> list[tuple[int, int]]:
    """Return a strictly monotonic 1:1 event alignment.

    Structural overlap can establish a match without lexical agreement; text
    can establish a match after moderate timing drift. Split/merge errors stay
    visible because one event can never consume multiple events.
    """
    n, m = len(ground), len(hypothesis)
    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    choices: list[list[str | None]] = [[None] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            best = dp[i + 1][j]
            choice = "skip_ground"
            if dp[i][j + 1] > best:
                best = dp[i][j + 1]
                choice = "skip_hypothesis"
            gold_segment = ground[i]
            hypothesis_segment = hypothesis[j]
            overlap = _intersection_over_union(gold_segment, hypothesis_segment)
            similarity = _text_similarity(gold_segment["text"], hypothesis_segment["text"])
            gold_midpoint = (float(gold_segment["start"]) + float(gold_segment["end"])) / 2
            hyp_midpoint = (float(hypothesis_segment["start"]) + float(hypothesis_segment["end"])) / 2
            midpoint_distance = abs(gold_midpoint - hyp_midpoint)
            both_vocal = (
                gold_segment["event_type"] in {"vocalization", "mixed"}
                and hypothesis_segment["event_type"] in {"vocalization", "mixed"}
            )
            eligible = overlap > 0 or similarity >= 0.45 or (both_vocal and midpoint_distance <= 3.0)
            if eligible:
                type_match = gold_segment["event_type"] == hypothesis_segment["event_type"]
                reward = (
                    1.0
                    + 2.0 * overlap
                    + similarity
                    + (0.5 if type_match else 0.0)
                    - min(midpoint_distance / 20.0, 0.75)
                    + dp[i + 1][j + 1]
                )
                if reward > best + 1e-12:
                    best = reward
                    choice = "match"
            dp[i][j] = best
            choices[i][j] = choice
    pairs: list[tuple[int, int]] = []
    i = j = 0
    while i < n and j < m:
        choice = choices[i][j]
        if choice == "match":
            pairs.append((i, j))
            i += 1
            j += 1
        elif choice == "skip_ground":
            i += 1
        else:
            j += 1
    return pairs


def _percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if quantile == 0.5:
        return float(median(ordered))
    index = max(0, min(len(ordered) - 1, math.ceil(quantile * len(ordered)) - 1))
    return float(ordered[index])


def _prf(true_positive: int, false_positive: int, false_negative: int) -> dict[str, Any]:
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 1.0
    recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def score_segments(
    ground: Sequence[dict[str, Any]],
    hypothesis: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    wer, cer = error_rates(_segment_text(ground), _segment_text(hypothesis))
    pairs = monotonic_alignment(ground, hypothesis)
    matched_ground = {ground_index for ground_index, _ in pairs}
    matched_hypothesis = {hypothesis_index for _, hypothesis_index in pairs}
    event_prf = _prf(
        len(pairs),
        len(hypothesis) - len(pairs),
        len(ground) - len(pairs),
    )

    vocal_types = {"vocalization", "mixed"}
    vocal_tp = vocal_fp = vocal_fn = 0
    for ground_index, hypothesis_index in pairs:
        gold_positive = ground[ground_index]["event_type"] in vocal_types
        hyp_positive = hypothesis[hypothesis_index]["event_type"] in vocal_types
        if gold_positive and hyp_positive:
            vocal_tp += 1
        elif hyp_positive:
            vocal_fp += 1
        elif gold_positive:
            vocal_fn += 1
    vocal_fn += sum(
        ground[index]["event_type"] in vocal_types
        for index in range(len(ground))
        if index not in matched_ground
    )
    vocal_fp += sum(
        hypothesis[index]["event_type"] in vocal_types
        for index in range(len(hypothesis))
        if index not in matched_hypothesis
    )

    onset_errors = [
        abs(float(ground[ground_index]["start"]) - float(hypothesis[hypothesis_index]["start"]))
        for ground_index, hypothesis_index in pairs
    ]
    end_errors = [
        abs(float(ground[ground_index]["end"]) - float(hypothesis[hypothesis_index]["end"]))
        for ground_index, hypothesis_index in pairs
    ]
    tolerances: dict[str, Any] = {}
    for tolerance in BOUNDARY_TOLERANCES_S:
        key = f"{int(tolerance * 1000)}ms"
        denominator = len(pairs)
        onset_hits = sum(error <= tolerance + 1e-12 for error in onset_errors)
        end_hits = sum(error <= tolerance + 1e-12 for error in end_errors)
        both_hits = sum(
            onset <= tolerance + 1e-12 and end <= tolerance + 1e-12
            for onset, end in zip(onset_errors, end_errors)
        )
        tolerances[key] = {
            "onset_recall": onset_hits / denominator if denominator else 0.0,
            "end_recall": end_hits / denominator if denominator else 0.0,
            "both_recall": both_hits / denominator if denominator else 0.0,
        }
    return {
        "wer": wer,
        "cer": cer,
        "alignment": {"monotonic": True, "pairs": pairs, **event_prf},
        "event_count": {
            "gold": len(ground),
            "predicted": len(hypothesis),
            "absolute_error": abs(len(ground) - len(hypothesis)),
            **event_prf,
        },
        "vocalization": _prf(vocal_tp, vocal_fp, vocal_fn),
        "boundaries": {
            "matched": len(pairs),
            "onset_mae_s": mean(onset_errors) if onset_errors else None,
            "onset_p90_s": _percentile(onset_errors, 0.9),
            "end_mae_s": mean(end_errors) if end_errors else None,
            "end_p90_s": _percentile(end_errors, 0.9),
            "tolerances": tolerances,
        },
    }


def _load_case_artifact(root: Path, descriptor: dict[str, Any]) -> dict[str, Any]:
    data = read_json(_resolve_artifact(root, descriptor["path"]))
    assert isinstance(data, dict)
    return data


def _aggregate_rows(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    ground_text = " ".join(row["ground_text"] for row in rows)
    hypothesis_text = " ".join(row["hypothesis_text"] for row in rows)
    wer, cer = error_rates(ground_text, hypothesis_text)
    matched = sum(row["metrics"]["alignment"]["true_positive"] for row in rows)
    event_fp = sum(row["metrics"]["alignment"]["false_positive"] for row in rows)
    event_fn = sum(row["metrics"]["alignment"]["false_negative"] for row in rows)
    vocal_tp = sum(row["metrics"]["vocalization"]["true_positive"] for row in rows)
    vocal_fp = sum(row["metrics"]["vocalization"]["false_positive"] for row in rows)
    vocal_fn = sum(row["metrics"]["vocalization"]["false_negative"] for row in rows)
    onset_errors = [value for row in rows for value in row["onset_errors"]]
    end_errors = [value for row in rows for value in row["end_errors"]]
    tolerance_metrics: dict[str, Any] = {}
    for tolerance in BOUNDARY_TOLERANCES_S:
        key = f"{int(tolerance * 1000)}ms"
        denominator = len(onset_errors)
        tolerance_metrics[key] = {
            "onset_recall": sum(value <= tolerance + 1e-12 for value in onset_errors) / denominator if denominator else 0.0,
            "end_recall": sum(value <= tolerance + 1e-12 for value in end_errors) / denominator if denominator else 0.0,
            "both_recall": sum(
                onset <= tolerance + 1e-12 and end <= tolerance + 1e-12
                for onset, end in zip(onset_errors, end_errors)
            ) / denominator if denominator else 0.0,
        }
    operator_values = [
        row["operator_review_minutes"] for row in rows
        if row["operator_review_minutes"] is not None
        and row.get("operator_verified")
    ]
    cost_values = [
        row["cost_usd"] for row in rows
        if row["cost_usd"] is not None and row.get("cost_verified")
    ]
    count = len(rows)
    return {
        "cases": count,
        "wer": wer,
        "cer": cer,
        "alignment": {"monotonic": True, **_prf(matched, event_fp, event_fn)},
        "event_count": {
            "gold": sum(row["metrics"]["event_count"]["gold"] for row in rows),
            "predicted": sum(row["metrics"]["event_count"]["predicted"] for row in rows),
            "absolute_error": sum(row["metrics"]["event_count"]["absolute_error"] for row in rows),
            **_prf(matched, event_fp, event_fn),
        },
        "vocalization": _prf(vocal_tp, vocal_fp, vocal_fn),
        "boundaries": {
            "matched": len(onset_errors),
            "onset_mae_s": mean(onset_errors) if onset_errors else None,
            "onset_p90_s": _percentile(onset_errors, 0.9),
            "end_mae_s": mean(end_errors) if end_errors else None,
            "end_p90_s": _percentile(end_errors, 0.9),
            "tolerances": tolerance_metrics,
        },
        "operator": {
            "coverage_count": len(operator_values),
            "coverage": len(operator_values) / count if count else 0.0,
            "p50_minutes": _percentile(operator_values, 0.5),
            "p90_minutes": _percentile(operator_values, 0.9),
        },
        "cost": {
            "coverage_count": len(cost_values),
            "coverage": len(cost_values) / count if count else 0.0,
            "total_usd": sum(cost_values) if cost_values else None,
            "mean_usd": mean(cost_values) if cost_values else None,
        },
    }


def _paired_cost_confidence(rows_by_system: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    current = {
        row["case_id"]: row["cost_usd"]
        for row in rows_by_system["current"] if row["cost_verified"]
    }
    candidate = {
        row["case_id"]: row["cost_usd"]
        for row in rows_by_system["candidate"] if row["cost_verified"]
    }
    differences = [
        float(candidate[case_id]) - float(current[case_id])
        for case_id in sorted(set(current) & set(candidate))
        if current[case_id] is not None and candidate[case_id] is not None
    ]
    if not differences:
        return {"pairs": 0, "mean_delta_usd": None, "ci95": None, "saves_money": False}
    average = mean(differences)
    if len(differences) < 2:
        interval = None
    else:
        standard_error = statistics.stdev(differences) / math.sqrt(len(differences))
        interval = [average - 1.96 * standard_error, average + 1.96 * standard_error]
    return {
        "pairs": len(differences),
        "mean_delta_usd": average,
        "ci95": interval,
        "saves_money": bool(interval and interval[1] < 0),
    }


def _wilson_lower(successes: int, total: int, *, z: float = 1.6448536269514722) -> float:
    """One-sided 95% Wilson lower confidence bound for a proportion."""
    if total <= 0 or successes < 0 or successes > total:
        return 0.0
    observed = successes / total
    denominator = 1.0 + (z * z) / total
    centre = observed + (z * z) / (2.0 * total)
    margin = z * math.sqrt(
        (observed * (1.0 - observed) / total)
        + (z * z) / (4.0 * total * total)
    )
    return max(0.0, (centre - margin) / denominator)


def _shadow_evidence(root: Path, manifest: dict[str, Any],
                     candidate: dict[str, Any]) -> dict[str, Any]:
    """Derive release evidence from an attested row ledger, never counters."""
    empty = {
        "attested": False, "bound": False, "consistent": False,
        "eligible": 0, "approved": 0, "correct": 0, "catastrophic": 0,
        "reviewed_approvals": 0, "duration_days": 0.0,
        "precision": 0.0, "precision_lower_95": 0.0, "coverage": 0.0,
    }
    shadow = manifest.get("shadow_evaluation")
    if not isinstance(shadow, dict) or not isinstance(shadow.get("ledger"), dict):
        return empty
    try:
        ledger = _load_case_artifact(root, shadow["ledger"])
    except (OSError, ValueError, KeyError):
        return empty
    if not isinstance(ledger, dict):
        return empty
    attested, _attestation_reason = verify_artifact(
        ledger, "BENCHMARK_SHADOW_PUBLIC_KEYS",
    )
    bound = bool(
        ledger.get("candidate_release") == candidate.get("release")
        and ledger.get("candidate_config_sha256") == candidate.get("config_sha256")
        and ledger.get("pipeline_config_fingerprint")
        == candidate.get("pipeline_config_fingerprint")
    )
    rows = ledger.get("decisions") or []
    eligible_rows = [row for row in rows if row.get("eligible") is True]
    approved_rows = [row for row in eligible_rows if row.get("would_approve") is True]
    reviewed_rows = [row for row in approved_rows if row.get("reviewed") is True]
    correct = sum(row.get("correct") is True for row in reviewed_rows)
    catastrophic = sum(row.get("catastrophic") is True for row in reviewed_rows)
    timestamps = [
        parsed for parsed in (
            _parse_utc_timestamp(row.get("occurred_at"))
            for row in eligible_rows
        )
        if parsed is not None
    ]
    now = datetime.now(timezone.utc)
    timestamps_valid = bool(
        timestamps and all(value <= now for value in timestamps)
    )
    duration_days = (
        (max(timestamps) - min(timestamps)).total_seconds() / 86400.0
        if len(timestamps) >= 2 else 0.0
    )
    approved = len(approved_rows)
    reviewed = len(reviewed_rows)
    consistent = bool(
        all(isinstance(row, dict) for row in rows)
        and len({row.get("decision_id") for row in rows}) == len(rows)
        and timestamps_valid
        and reviewed == approved
        and correct <= reviewed
        and catastrophic <= reviewed
    )
    precision = correct / reviewed if reviewed else 0.0
    return {
        "attested": attested,
        "bound": bound,
        "consistent": consistent,
        "eligible": len(eligible_rows),
        "approved": approved,
        "correct": correct,
        "catastrophic": catastrophic,
        "reviewed_approvals": reviewed,
        "duration_days": duration_days,
        "precision": precision,
        "precision_lower_95": _wilson_lower(correct, reviewed),
        "coverage": approved / len(eligible_rows) if eligible_rows else 0.0,
    }


def _release_gate(
    manifest: dict[str, Any],
    systems: dict[str, Any],
    rows_by_system: dict[str, list[dict[str, Any]]],
    *,
    root: Path,
) -> dict[str, Any]:
    """Evaluate every rollout criterion; absent evidence is always NO-GO."""
    targets = RELEASE_TARGETS
    entries = manifest["entries"]
    category_counts = {
        category: sum(entry.get("category") == category for entry in entries)
        for category in ("live", "studio", "adversarial")
    }
    def tagged(wanted: set[str]) -> int:
        return sum(
            bool(set(entry.get("tags") or []) & wanted) for entry in entries
        )
    candidate_all = systems["candidate"]["all"]
    candidate_live = systems["candidate"].get("live") or _aggregate_rows([])
    candidate_holdout = systems["candidate"]["holdout"]
    current_all = systems["current"]["all"]
    current_holdout = systems["current"]["holdout"]
    shadow = _shadow_evidence(root, manifest, systems["candidate"])
    decisions = shadow["eligible"]
    approved = shadow["approved"]
    catastrophic = shadow["catastrophic"]
    shadow_consistent = shadow["consistent"]
    precision = shadow["precision"]
    precision_lower = shadow["precision_lower_95"]
    coverage = shadow["coverage"]
    shadow_bound = shadow["bound"]
    cost = _paired_cost_confidence(rows_by_system)

    pericos_cases = [
        entry for entry in entries
        if entry.get("regression_fixture") == "los_pericos"
    ]
    pericos_ok = False
    if len(pericos_cases) == 1:
        case_id = pericos_cases[0]["case_id"]
        candidate_row = next(
            (row for row in rows_by_system["candidate"] if row["case_id"] == case_id),
            None,
        )
        if candidate_row:
            metrics = candidate_row["metrics"]
            texts = [_normalise_text(text) for text in candidate_row["segment_texts"]]
            times = candidate_row["segment_times"]
            expected = [
                (60.85, 63.77), (63.77, 67.04), (67.05, 73.17),
                (73.18, 75.65), (75.65, 75.75), (79.31, 83.27),
            ]
            pericos_ok = bool(
                metrics["event_count"]["predicted"] == 6
                and metrics["event_count"]["absolute_error"] == 0
                and len(texts) == 6
                and len(times) == len(expected)
                and all(text.startswith("real") for text in texts[:4])
                and texts[4].startswith("no")
                and texts[5].startswith("no")
                and not texts[5].startswith("real")
                and all(
                    abs(float(actual["start"]) - target_start) <= .50
                    and abs(float(actual["end"]) - target_end) <= .75
                    for actual, (target_start, target_end) in zip(times, expected)
                )
            )

    checks = {
        "corpus_50": len(entries) == targets["cases"],
        "split_30_20": (
            sum(entry["split"] == "dev" for entry in entries) == targets["dev_cases"]
            and sum(entry["split"] == "holdout" for entry in entries)
            == targets["holdout_cases"]
        ),
        "cohorts_20_20_10": category_counts == {
            "live": targets["live_cases"], "studio": targets["studio_cases"],
            "adversarial": targets["adversarial_cases"],
        },
        "repetition_adlib_coverage": tagged({"repetition", "adlib"})
        >= targets["repetition_or_adlib_cases"],
        "crowd_chorus_coverage": tagged({"crowd", "chorus"})
        >= targets["crowd_or_chorus_cases"],
        "pericos_six_events": pericos_ok,
        "event_count_f1_global": candidate_all["event_count"]["f1"]
        >= targets["event_count_f1"],
        "event_count_f1_live": candidate_live["event_count"]["f1"]
        >= targets["event_count_f1"],
        "event_count_f1_holdout": candidate_holdout["event_count"]["f1"]
        >= targets["event_count_f1"],
        "vocalization_recall_global": candidate_all["vocalization"]["recall"]
        >= targets["vocalization_recall"],
        "vocalization_recall_live": candidate_live["vocalization"]["recall"]
        >= targets["vocalization_recall"],
        "vocalization_recall_holdout": candidate_holdout["vocalization"]["recall"]
        >= targets["vocalization_recall"],
        "timing_onset_p90": candidate_all["boundaries"]["onset_p90_s"] is not None
        and candidate_all["boundaries"]["onset_p90_s"] <= targets["onset_p90_s"],
        "timing_end_p90": candidate_all["boundaries"]["end_p90_s"] is not None
        and candidate_all["boundaries"]["end_p90_s"] <= targets["end_p90_s"],
        "timing_holdout_p90": (
            candidate_holdout["boundaries"]["onset_p90_s"] is not None
            and candidate_holdout["boundaries"]["end_p90_s"] is not None
            and candidate_holdout["boundaries"]["onset_p90_s"] <= targets["onset_p90_s"]
            and candidate_holdout["boundaries"]["end_p90_s"] <= targets["end_p90_s"]
        ),
        "wer_non_regression": candidate_all["wer"]
        <= current_all["wer"] + targets["wer_regression_absolute"],
        "wer_holdout_non_regression": candidate_holdout["wer"]
        <= current_holdout["wer"] + targets["wer_regression_absolute"],
        "candidate_runtime_config_bound": _is_runtime_fingerprint(
            systems["candidate"].get("pipeline_config_fingerprint")
        ),
        "shadow_ledger_attested": shadow["attested"],
        "shadow_counts_consistent": shadow_consistent,
        "shadow_bound_to_candidate": shadow_bound,
        "automatic_precision": precision_lower >= targets["automatic_precision"],
        "zero_catastrophic_approvals": catastrophic == 0 and approved > 0,
        "automatic_coverage": coverage >= targets["automatic_coverage"],
        "shadow_volume_and_duration": shadow_consistent
        and decisions >= targets["shadow_decisions"]
        and shadow["duration_days"] >= targets["shadow_days"],
        "operator_full_coverage": candidate_all["operator"]["coverage"] == 1.0,
        "operator_p50": candidate_all["operator"]["p50_minutes"] is not None
        and candidate_all["operator"]["p50_minutes"] < targets["operator_p50_minutes"],
        "operator_p90": candidate_all["operator"]["p90_minutes"] is not None
        and candidate_all["operator"]["p90_minutes"] < targets["operator_p90_minutes"],
        "cost_full_coverage": cost["pairs"] == len(entries) and bool(entries),
        "cost_ci95_below_baseline": cost["saves_money"],
    }
    blockers = [name for name, passed in checks.items() if not passed]
    return {
        "decision": "GO" if not blockers else "NO_GO",
        "checks": checks,
        "blockers": blockers,
        "targets": targets,
        "observed": {
            "cases": len(entries), "categories": category_counts,
            "automatic_precision": precision,
            "automatic_precision_lower_95": precision_lower,
            "automatic_coverage": coverage,
            "shadow_decisions": decisions,
            "shadow_days": shadow["duration_days"],
            "reviewed_approvals": shadow["reviewed_approvals"],
            "catastrophic_approvals": catastrophic,
            "shadow_ledger_attested": shadow["attested"],
            "shadow_counts_consistent": shadow_consistent,
            "shadow_bound_to_candidate": shadow_bound,
            "paired_cost": cost,
        },
    }


def score_manifest(manifest_path: Path) -> dict[str, Any]:
    manifest_path = Path(manifest_path)
    manifest = load_validated_manifest(manifest_path)
    root = manifest_path.parent
    per_case: list[dict[str, Any]] = []
    rows_by_system: dict[str, list[dict[str, Any]]] = {name: [] for name in SYSTEMS}
    for entry in manifest["entries"]:
        gold = _load_case_artifact(root, entry["gold"])
        ground = gold["segments"]
        case_result: dict[str, Any] = {
            "case_id": entry["case_id"],
            "split": entry["split"],
            "systems": {},
        }
        for system_name in SYSTEMS:
            output = _load_case_artifact(root, entry["outputs"][system_name])
            hypothesis = output["segments"]
            metrics = score_segments(ground, hypothesis)
            case_result["systems"][system_name] = metrics
            pairs = metrics["alignment"]["pairs"]
            rows_by_system[system_name].append(
                {
                    "case_id": entry["case_id"],
                    "split": entry["split"],
                    "ground_text": _segment_text(ground),
                    "hypothesis_text": _segment_text(hypothesis),
                    "metrics": metrics,
                    "onset_errors": [
                        abs(float(ground[ground_index]["start"]) - float(hypothesis[hypothesis_index]["start"]))
                        for ground_index, hypothesis_index in pairs
                    ],
                    "end_errors": [
                        abs(float(ground[ground_index]["end"]) - float(hypothesis[hypothesis_index]["end"]))
                        for ground_index, hypothesis_index in pairs
                    ],
                    "operator_review_minutes": output["operator_review_minutes"],
                    "operator_verified": (
                        output["operator_review_minutes"] is None
                        or isinstance(output.get("operator_evidence"), dict)
                    ),
                    "cost_usd": output["cost_usd"],
                    "cost_verified": (
                        output["cost_usd"] is None
                        or isinstance(output.get("cost_evidence"), dict)
                    ),
                    "segment_texts": [str(item.get("text") or "") for item in hypothesis],
                    "segment_times": [
                        {"start": float(item["start"]), "end": float(item["end"])}
                        for item in hypothesis
                    ],
                }
            )
        per_case.append(case_result)

    system_results: dict[str, Any] = {}
    for system_name, rows in rows_by_system.items():
        config_payload = _load_case_artifact(
            root, manifest["systems"][system_name]["config"],
        )
        system_results[system_name] = {
            "release": manifest["systems"][system_name]["release"],
            "config_sha256": manifest["systems"][system_name]["config"]["sha256"],
            "pipeline_config_fingerprint": config_payload.get(
                "pipeline_config_fingerprint"
            ) if isinstance(config_payload, dict) else None,
            "all": _aggregate_rows(rows),
            "dev": _aggregate_rows([row for row in rows if row["split"] == "dev"]),
            "holdout": _aggregate_rows([row for row in rows if row["split"] == "holdout"]),
            "live": _aggregate_rows([
                row for row in rows if row.get("category") == "live"
            ]),
        }
    # Category is benchmark metadata, never inferred from artist/title.
    categories = {entry["case_id"]: entry.get("category") for entry in manifest["entries"]}
    for rows in rows_by_system.values():
        for row in rows:
            row["category"] = categories.get(row["case_id"])
    # Recompute category views now that metadata is attached.
    for system_name, rows in rows_by_system.items():
        system_results[system_name]["live"] = _aggregate_rows([
            row for row in rows if row.get("category") == "live"
        ])
    release_gate = _release_gate(
        manifest, system_results, rows_by_system, root=root,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "benchmark_id": manifest["benchmark_id"],
        "manifest_sha256": sha256_file(manifest_path),
        "systems": system_results,
        "per_case": per_case,
        "release_gate": release_gate,
    }


def format_validation(errors: Iterable[str]) -> str:
    return "\n".join(f"- {error}" for error in errors)
