"""Fail-closed no-render validation gates."""
import base64
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from correction_learning import sha256_json, validate_proposal_config
from evidence_attestation import sign_artifact
from quality_learning_jobs import (
    _load_validation_report, _validate_report, run_daily_quality_learning,
)


def _report(proposal_id="proposal-1", config=None):
    config = config or {"prefer_mix_witness": True}
    return {
        "release_gate": {"decision": "GO"},
        "quality_learning": {
            "proposal_id": proposal_id,
            "candidate_config_sha256": sha256_json(config),
            "render": False, "one_variable_ablation": True,
            "target_relative_reduction": 0.25,
            "wer_delta_percentage_points": 0.5,
            "temporal_integrity": True,
            "cost_delta_ci_high_usd": -0.01,
            "baseline_config_sha256": "a" * 64,
            "audio_manifest_sha256": "b" * 64,
            "comparators": ["baseline", "candidate", "ROTOR"],
            "veo_calls": 0,
        },
    }


@pytest.mark.parametrize("config", [
    {},
    {"enable_second_asr": True, "enable_acoustic_dp": True},
    {"unknown": True},
    {"enable_second_asr": 1},
    {"event_boundary_margin_ms": 5000},
    {"stem_mix_disagreement_threshold": float("nan")},
])
def test_ablation_configuration_is_exactly_one_typed_bounded_variable(config):
    with pytest.raises(ValueError):
        validate_proposal_config(config)


def test_ablation_configuration_accepts_one_safe_variable():
    assert validate_proposal_config({"event_boundary_margin_ms": 250}) == {
        "event_boundary_margin_ms": 250,
    }


def test_disabled_daily_miner_still_schedules_next_wakeup(monkeypatch):
    monkeypatch.setenv("QUALITY_LEARNING_MINING_ENABLED", "0")
    monkeypatch.setattr(
        "queue_jobs.ensure_daily_quality_learning_scheduled",
        lambda: "quality-learning:daily:next",
    )
    result = run_daily_quality_learning()
    assert result == {
        "disabled": True,
        "next_job_id": "quality-learning:daily:next",
    }


def test_validation_requires_every_business_and_v5_gate():
    report = _report()
    result = _validate_report(
        report, "proposal-1", sha256_json({"prefer_mix_witness": True}),
    )
    assert result["passed"] is True
    report["quality_learning"]["target_relative_reduction"] = 0.19
    assert _validate_report(
        report, "proposal-1", sha256_json({"prefer_mix_witness": True}),
    )["passed"] is False


@pytest.mark.parametrize("field,value", [
    ("render", True),
    ("proposal_id", "another-proposal"),
    ("candidate_config_sha256", "0" * 64),
])
def test_validation_rejects_unbound_or_rendering_report(field, value):
    report = _report()
    report["quality_learning"][field] = value
    with pytest.raises(ValueError):
        _validate_report(
            report, "proposal-1", sha256_json({"prefer_mix_witness": True}),
        )


def test_validation_report_must_be_signed(tmp_path, monkeypatch):
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
    path = tmp_path / "report.json"
    path.write_text(json.dumps(_report()), encoding="utf-8")
    monkeypatch.setenv("BENCHMARK_RELEASE_PUBLIC_KEYS", json.dumps({
        "benchmark-key": base64.b64encode(public_raw).decode(),
    }))
    with pytest.raises(ValueError, match="attestation"):
        _load_validation_report(path)
    signed = sign_artifact(
        _report(), base64.b64encode(private_raw).decode(), "benchmark-key",
    )
    path.write_text(json.dumps(signed), encoding="utf-8")
    loaded, digest = _load_validation_report(path)
    assert loaded["quality_learning"]["render"] is False
    assert len(digest) == 64
