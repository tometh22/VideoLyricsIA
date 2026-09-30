"""Staging-only kill switches: delivery QC and the language review stop gating,
and stay completely inert anywhere else (production above all)."""
from types import SimpleNamespace

import pytest

import delivery_qc_runtime as qc
import language_review

JOB = SimpleNamespace(job_id="j", status="pending_review", workload_class="batch", s3_keys={}, umg_spec=None,
                      delivery_profile=None, prores_ready=False, campaign_id="c", segments_json=[], transcription_quality=None)


def blocked_gate():
    return qc.delivery_readiness_gate(JOB, None, for_umg_delivery=True)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("ENVIRONMENT", "DELIVERY_QC_STAGING_GATES_OFF", "LANGUAGE_REVIEW_STAGING_ADVISORY"):
        monkeypatch.delenv(name, raising=False)


def test_delivery_gate_blocks_by_default():
    gate = blocked_gate()
    assert gate["blocked"] is True and gate["reason"] == "fresh_preflight_required"


def test_delivery_gate_never_blocks_when_staging_switch_is_on(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("DELIVERY_QC_STAGING_GATES_OFF", "1")
    gate = blocked_gate()
    assert gate["blocked"] is False and gate["can_approve"] is True
    assert gate["reason"] == "staging_delivery_gates_off" and gate["staging_gates_off"] is True
    # Also without any report at all and without the UMG hint: nothing gates.
    assert qc.delivery_readiness_gate(JOB, {"status": "STALE"})["blocked"] is False


@pytest.mark.parametrize("environment", ["", "production", "prod", "development", "Staging-copy"])
def test_delivery_switch_is_inert_outside_staging(monkeypatch, environment):
    monkeypatch.setenv("ENVIRONMENT", environment)
    monkeypatch.setenv("DELIVERY_QC_STAGING_GATES_OFF", "1")
    assert qc.staging_delivery_gates_off() is False
    assert blocked_gate()["blocked"] is True


def test_delivery_switch_needs_the_explicit_flag(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "staging")
    assert qc.staging_delivery_gates_off() is False
    monkeypatch.setenv("DELIVERY_QC_STAGING_GATES_OFF", "0")
    assert qc.staging_delivery_gates_off() is False
    assert blocked_gate()["blocked"] is True


def test_language_review_advisory_follows_the_same_rules(monkeypatch):
    assert language_review.staging_advisory() is False
    monkeypatch.setenv("LANGUAGE_REVIEW_STAGING_ADVISORY", "1")
    assert language_review.staging_advisory() is False            # not staging
    monkeypatch.setenv("ENVIRONMENT", "production")
    assert language_review.staging_advisory() is False
    monkeypatch.setenv("ENVIRONMENT", "staging")
    assert language_review.staging_advisory() is True
    monkeypatch.delenv("LANGUAGE_REVIEW_STAGING_ADVISORY")
    assert language_review.staging_advisory() is False


def test_language_review_is_still_computed_when_advisory(monkeypatch):
    """Advisory means 'warn, don't block': the discrepancy is still reported."""
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("LANGUAGE_REVIEW_STAGING_ADVISORY", "1")
    payload = language_review.review_payload([], None, 0)
    assert "needs_language_review" in payload and "language_review_resolved" in payload
