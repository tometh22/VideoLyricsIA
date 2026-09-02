"""Offline contract tests for the fail-closed quality-v6 scaffold."""
from __future__ import annotations

import base64
import hashlib
import json
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from evidence_attestation import sign_artifact
from quality_v6_calibration import (
    ACTION_GATES,
    CALIBRATION_SCHEMA,
    DATASET_SCHEMA,
    DATA_REQUIREMENTS,
    PREDICTION_CERTIFICATION_SCHEMA,
    POLICY_VERSION,
    artifact_sha256,
    certify_offline_prediction,
    evaluate_action_gate,
    runtime_review_proposal_authorization,
    summarize_dataset,
    split_sha256,
    validate_calibration_artifact,
    validate_dataset_manifest,
    wilson_lower_bound,
)
from scripts.build_v6_dataset_manifest import build_manifest
from scripts.train_phone_event_model import create_training_plan, sha256_directory


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _keypair() -> tuple[str, str]:
    private = Ed25519PrivateKey.generate()
    private_raw = private.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public_raw = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return base64.b64encode(private_raw).decode(), base64.b64encode(public_raw).decode()


def _entry(split: str, category: str, index: int) -> dict[str, object]:
    case_id = f"{split}-{index:04d}"
    audio_hash = _sha(f"{case_id}:audio")
    entry: dict[str, object] = {
        "case_id": case_id,
        "split": split,
        "category": category,
        "identity": {
            "artist_sha256": _sha(f"{case_id}:artist"),
            "song_sha256": _sha(f"{case_id}:song"),
            "master_sha256": _sha(f"{case_id}:master"),
            "audio_sha256": audio_hash,
        },
        "audio": {
            "sha256": audio_hash,
            "duration_seconds": 900.0 if split == "training" else 180.0,
            "storage_uri": f"r2-private://quality-v6/{case_id}.wav",
        },
        "annotations": {
            "status": "adjudicated",
            "annotator_count": 2,
            "adjudicator_id_sha256": _sha(f"{case_id}:adjudicator"),
            "artifact_sha256": _sha(f"{case_id}:annotation"),
            "hierarchical": True,
            "event_count": 20,
        },
        "license": {
            "license_id": f"license-{case_id}",
            "license_name": "GenLy explicit model-training grant",
            "license_uri": "urn:genly:legal:model-training-v1",
            "rights_basis": "contract",
            "evidence_sha256": _sha(f"{case_id}:rights"),
            "commercial_use_allowed": True,
            "model_training_allowed": True,
            "global_training_allowed": True,
            "derivatives_allowed": True,
            "expires_at": None,
        },
    }
    if split == "training":
        entry["training_example"] = {
            "format": "phone-event-npz-v1",
            "path": f"examples/{case_id}.npz",
            "sha256": _sha(f"{case_id}:npz"),
            "sample_rate_hz": 16_000,
            "duration_seconds": 900.0,
            "event_count": 20,
        }
    return entry


def _entries() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    rows.extend(_entry("training", "live" if index < 50 else "studio", index) for index in range(100))
    regression_categories = ["live"] * 20 + ["studio"] * 20 + ["adversarial"] * 10
    rows.extend(_entry("regression", category, index) for index, category in enumerate(regression_categories))
    rows.extend(_entry("calibration", "live" if index % 2 == 0 else "studio", index) for index in range(300))
    rows.extend(_entry("temporal", "live" if index % 2 == 0 else "adversarial", index) for index in range(150))
    return rows


def _unsigned_manifest() -> dict[str, object]:
    entries = _entries()
    return {
        "schema": DATASET_SCHEMA,
        "policy_version": POLICY_VERSION,
        "status": "ready",
        "dataset_id": "quality-v6-test-corpus",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "contract": {
            "purpose": "offline_phone_event_training_calibration",
            "contains_customer_audio": True,
            "tenant_opt_in_required": True,
            "ambiguous_rights_excluded": True,
            "raw_content_tenant_scoped": True,
            "revocation_supported": True,
            "legal_review_id": "legal-review-2026-08",
            "revocation_process": "Rebuild manifests and all descendants after a rights revocation.",
        },
        "summary": summarize_dataset(entries),
        "entries": entries,
    }


def _signed_manifest(monkeypatch) -> tuple[dict[str, object], str]:
    private, public = _keypair()
    key_id = "dataset-test-key"
    monkeypatch.setenv("QUALITY_V6_DATASET_PUBLIC_KEYS", json.dumps({key_id: public}))
    return sign_artifact(_unsigned_manifest(), private, key_id), private


def _calibration_artifact(manifest: dict[str, object]) -> dict[str, object]:
    evidence = {}
    perfect_counts = {
        "suggestion": 300,
        "timing_reversible": 539,
        "content_reversible": 539,
        "structural": 3_000,
    }
    for action, count in perfect_counts.items():
        evidence[action] = {
            "correct": count,
            "reviewed": count,
            "catastrophic": 0,
            "wilson_lower_95": wilson_lower_bound(count, count),
        }
    summary = manifest["summary"]
    return {
        "schema": CALIBRATION_SCHEMA,
        "policy_version": POLICY_VERSION,
        "status": "offline_calibrated",
        "offline_only": True,
        "runtime_authorization": False,
        "automatic_apply_allowed": False,
        "dataset_manifest_sha256": artifact_sha256(manifest),
        "dataset_summary": summary,
        "model": {
            "architecture": "xls-r-phone-event-v1",
            "training_status": "trained_uncalibrated",
            "exported": False,
            "checkpoint_sha256": _sha("checkpoint"),
            "training_report_sha256": _sha("training-report"),
            "training_manifest_sha256": artifact_sha256(manifest),
            "training_cases": summary["split_counts"]["training"],
            "training_hours": summary["training_hours"],
            "training_events": summary["training_events"],
        },
        "conformal": {
            "cardinality": {
                "method": "split_conformal",
                "calibration_examples": 300,
                "empirical_total": 300,
                "empirical_covered": 297,
                "target_coverage": 0.99,
                "decision_contract": "singleton_only",
                "source_split_sha256": split_sha256(manifest, "calibration"),
            },
            "content": {
                "method": "raps",
                "calibration_examples": 300,
                "empirical_total": 300,
                "empirical_covered": 297,
                "target_coverage": 0.99,
                "decision_contract": "singleton_only",
                "source_split_sha256": split_sha256(manifest, "calibration"),
            },
            "timing": {
                "method": "conformalized_quantile",
                "calibration_examples": 300,
                "empirical_total": 300,
                "empirical_covered": 297,
                "target_coverage": 0.99,
                "decision_contract": "bounded_intervals",
                "coverage_scope": "joint_onset_end",
                "source_split_sha256": split_sha256(manifest, "calibration"),
            },
        },
        "temporal_evaluation": {
            "cases": 150,
            "catastrophic": 0,
            "source_split_sha256": split_sha256(manifest, "temporal"),
        },
        "action_evidence": evidence,
    }


def _signed_calibration(manifest: dict[str, object], monkeypatch) -> dict[str, object]:
    private, public = _keypair()
    key_id = "calibration-test-key"
    monkeypatch.setenv("QUALITY_V6_CALIBRATION_PUBLIC_KEYS", json.dumps({key_id: public}))
    return sign_artifact(_calibration_artifact(manifest), private, key_id)


def test_requirements_encode_all_three_evaluation_corpora():
    assert DATA_REQUIREMENTS["regression_cases"] == 50
    assert DATA_REQUIREMENTS["regression_categories"] == {
        "live": 20, "studio": 20, "adversarial": 10,
    }
    assert DATA_REQUIREMENTS["calibration_cases_min"] == 300
    assert DATA_REQUIREMENTS["temporal_cases_min"] == 150


def test_unsigned_dataset_is_rejected_even_when_adequate(monkeypatch):
    monkeypatch.delenv("QUALITY_V6_DATASET_PUBLIC_KEYS", raising=False)
    errors = validate_dataset_manifest(_unsigned_manifest())
    assert any("attestation rejected" in error for error in errors)


def test_signed_adequate_dataset_passes(monkeypatch):
    manifest, _private = _signed_manifest(monkeypatch)
    assert validate_dataset_manifest(manifest) == []


def test_dataset_rejects_cross_split_identity_leakage():
    manifest = _unsigned_manifest()
    manifest["entries"][100]["identity"]["master_sha256"] = manifest["entries"][0]["identity"]["master_sha256"]
    manifest["summary"] = summarize_dataset(manifest["entries"])
    errors = validate_dataset_manifest(manifest, require_signature=False)
    assert any("master_sha256 leakage across training/regression" in error for error in errors)


def test_dataset_rejects_ambiguous_or_noncommercial_rights():
    manifest = _unsigned_manifest()
    manifest["entries"][0]["license"]["global_training_allowed"] = False
    errors = validate_dataset_manifest(manifest, require_signature=False)
    assert "entries[0].license.global_training_allowed must be exactly true" in errors


def test_builder_draft_is_explicitly_untrusted():
    unsigned = _unsigned_manifest()
    inventory = {
        "dataset_id": unsigned["dataset_id"],
        "contract": unsigned["contract"],
        "entries": unsigned["entries"],
    }
    draft = build_manifest(inventory, draft=True)
    assert draft["status"] == "draft"
    assert "attestation" not in draft
    assert any("attestation" in error or "status" in error for error in validate_dataset_manifest(draft))


@pytest.mark.parametrize(
    "action,count,expected",
    [
        ("suggestion", 300, True),
        ("timing_reversible", 538, False),
        ("timing_reversible", 539, True),
        ("content_reversible", 539, True),
        ("structural", 2_999, False),
        ("structural", 3_000, True),
    ],
)
def test_action_specific_wilson_gates(action, count, expected):
    result = evaluate_action_gate(action, correct=count, reviewed=count)
    assert result["passed"] is expected
    assert result["target"] == ACTION_GATES[action]


def test_one_catastrophic_result_closes_every_action_gate():
    result = evaluate_action_gate(
        "structural", correct=2_999, reviewed=3_000, catastrophic=1,
    )
    assert result["passed"] is False
    assert "catastrophic_action" in result["blockers"]


def test_signed_calibration_and_singletons_allow_review_only(monkeypatch):
    manifest, _private = _signed_manifest(monkeypatch)
    artifact = _signed_calibration(manifest, monkeypatch)
    assert validate_calibration_artifact(artifact, manifest) == []
    decision = certify_offline_prediction(
        artifact,
        manifest,
        action="timing_reversible",
        cardinality_candidates=[6],
        content_candidates=["candidate-hash-or-private-text"],
        onset_interval=[60.80, 61.00],
        end_interval=[63.50, 63.80],
    )
    assert decision["eligible_offline"] is True
    assert decision["kind"] == "review_proposal_certification"
    assert decision["schema"] == PREDICTION_CERTIFICATION_SCHEMA
    assert decision["review_proposal_allowed"] is True
    assert decision["automatic_apply_allowed"] is False
    assert decision["runtime_authorization"] is False


def test_conformal_sets_and_timing_fail_closed(monkeypatch):
    manifest, _private = _signed_manifest(monkeypatch)
    artifact = _signed_calibration(manifest, monkeypatch)
    decision = certify_offline_prediction(
        artifact,
        manifest,
        action="structural",
        cardinality_candidates=[6, 7],
        content_candidates=["candidate-a", "candidate-b"],
        onset_interval=[60.0, 60.6],
        end_interval=[60.5, 61.5],
    )
    assert decision["eligible_offline"] is False
    assert "cardinality_conformal_set_not_singleton" in decision["blockers"]
    assert "content_conformal_set_not_singleton" in decision["blockers"]
    assert "onset_interval_too_wide" in decision["blockers"]
    assert "end_interval_too_wide" in decision["blockers"]
    assert "timing_intervals_do_not_define_positive_duration" in decision["blockers"]


def test_runtime_certification_malformed_sequences_abstain_without_throwing(monkeypatch):
    monkeypatch.setenv("QUALITY_V6_PROPOSALS_ENABLED", "1")
    result = runtime_review_proposal_authorization({
        "kind": "review_proposal_certification",
        "schema": PREDICTION_CERTIFICATION_SCHEMA,
        "policy_version": POLICY_VERSION,
        "cardinality_candidates": 6,
        "content_candidates": "private lyric",
        "onset_interval": {"start": 1},
        "end_interval": None,
    })
    assert result["authorized"] is False
    assert result["automatic_apply_allowed"] is False
    assert "cardinality_candidates_invalid" in result["blockers"]
    assert "content_candidates_invalid" in result["blockers"]


def test_tampering_with_calibration_invalidates_signature(monkeypatch):
    manifest, _private = _signed_manifest(monkeypatch)
    artifact = _signed_calibration(manifest, monkeypatch)
    artifact["temporal_evaluation"]["cases"] = 999
    errors = validate_calibration_artifact(artifact, manifest)
    assert any("attestation rejected" in error for error in errors)


def test_training_plan_is_local_only_and_never_claims_export(monkeypatch, tmp_path):
    manifest, _private = _signed_manifest(monkeypatch)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    local_model = tmp_path / "xls-r-local"
    local_model.mkdir()
    (local_model / "config.json").write_text('{"model_type":"wav2vec2"}', encoding="utf-8")
    model_hash = sha256_directory(local_model)
    transformers_before = sys.modules.get("transformers")
    plan = create_training_plan(
        manifest,
        manifest_path=manifest_path,
        base_model_path=local_model,
        base_model_sha256=model_hash,
        epochs=3,
        learning_rate=1e-5,
    )
    assert plan["status"] == "planned"
    assert plan["trained"] is False
    assert plan["calibrated"] is False
    assert plan["exported"] is False
    assert plan["runtime_authorization"] is False
    assert sys.modules.get("transformers") is transformers_before


def test_training_plan_refuses_inadequate_data(monkeypatch, tmp_path):
    manifest, _private = _signed_manifest(monkeypatch)
    reduced = deepcopy(manifest)
    reduced.pop("attestation")
    reduced["entries"] = reduced["entries"][:10]
    reduced["summary"] = summarize_dataset(reduced["entries"])
    private, public = _keypair()
    monkeypatch.setenv("QUALITY_V6_DATASET_PUBLIC_KEYS", json.dumps({"small": public}))
    reduced = sign_artifact(reduced, private, "small")
    local_model = tmp_path / "local"
    local_model.mkdir()
    (local_model / "config.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="inadequate"):
        create_training_plan(
            reduced,
            manifest_path=tmp_path / "manifest.json",
            base_model_path=local_model,
            base_model_sha256=sha256_directory(local_model),
            epochs=1,
            learning_rate=1e-5,
        )
