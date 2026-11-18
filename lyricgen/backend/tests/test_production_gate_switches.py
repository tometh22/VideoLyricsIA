"""Production-capable switches for the two approval gates that did not exist in the
production line (delivery QC/preflight and the language-review 409).

They exist so a promotion keeps the behaviour production had until the QC redesign:
explicit operator env vars, default OFF (the gates stay active), reported in /health.
The older staging-only switches keep working and stay inert outside staging.
"""
from types import SimpleNamespace

import pytest

import delivery_qc_runtime as qc
import language_review
import observability

JOB = SimpleNamespace(job_id="j", status="pending_review", workload_class="batch", s3_keys={}, umg_spec=None,
                      delivery_profile=None, prores_ready=False, campaign_id="c", segments_json=[], transcription_quality=None)
ALL = ("ENVIRONMENT", "DELIVERY_QC_GATES_OFF", "DELIVERY_QC_STAGING_GATES_OFF",
       "LANGUAGE_REVIEW_ADVISORY", "LANGUAGE_REVIEW_STAGING_ADVISORY")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ALL:
        monkeypatch.delenv(name, raising=False)


def gate():
    return qc.delivery_readiness_gate(JOB, None, for_umg_delivery=True)


def test_gates_stay_active_by_default_in_every_environment(monkeypatch):
    for environment in ("", "production", "prod", "staging"):
        monkeypatch.setenv("ENVIRONMENT", environment)
        assert qc.delivery_gates_off() is False and qc.delivery_gates_off_reason() is None
        assert gate()["blocked"] is True
        assert language_review.advisory() is False


@pytest.mark.parametrize("environment", ["", "production", "prod", "development", "staging"])
def test_delivery_switch_works_in_any_environment_and_says_so(monkeypatch, environment):
    monkeypatch.setenv("ENVIRONMENT", environment)
    monkeypatch.setenv("DELIVERY_QC_GATES_OFF", "1")
    result = gate()
    assert result["blocked"] is False and result["can_approve"] is True
    assert result["reason"] == "delivery_gates_off" and result["gates_off"] is True
    assert result["staging_gates_off"] is False
    # Nothing gates, even with no report and a stale one.
    assert qc.delivery_readiness_gate(JOB, {"status": "STALE"})["blocked"] is False


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_delivery_switch_needs_an_explicit_truthy_value(monkeypatch, value):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("DELIVERY_QC_GATES_OFF", value)
    assert qc.delivery_gates_off() is False and gate()["blocked"] is True


def test_the_old_staging_switch_still_works_only_in_staging(monkeypatch):
    monkeypatch.setenv("DELIVERY_QC_STAGING_GATES_OFF", "1")
    monkeypatch.setenv("ENVIRONMENT", "production")
    assert qc.delivery_gates_off() is False
    monkeypatch.setenv("ENVIRONMENT", "staging")
    result = gate()
    assert result["blocked"] is False and result["reason"] == "staging_delivery_gates_off"
    assert result["staging_gates_off"] is True


def test_language_advisory_switch_works_in_production_and_the_old_one_does_not(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("LANGUAGE_REVIEW_STAGING_ADVISORY", "1")
    assert language_review.advisory() is False                      # staging-only switch stays inert
    monkeypatch.setenv("LANGUAGE_REVIEW_ADVISORY", "1")
    assert language_review.advisory() is True                       # the production-capable one
    monkeypatch.delenv("LANGUAGE_REVIEW_ADVISORY")
    monkeypatch.setenv("ENVIRONMENT", "staging")
    assert language_review.advisory() is True                       # old staging behaviour preserved


def test_health_reports_which_gates_are_off(monkeypatch):
    def features():
        return observability.health_snapshot(enforce_fleet_readiness=False)["features"]
    assert features()["delivery_qc_gates_off"] is False and features()["language_review_advisory"] is False
    monkeypatch.setenv("DELIVERY_QC_GATES_OFF", "1")
    monkeypatch.setenv("LANGUAGE_REVIEW_ADVISORY", "1")
    assert features()["delivery_qc_gates_off"] is True and features()["language_review_advisory"] is True


def test_both_approval_paths_consult_the_production_capable_switch():
    """/approve and the campaign approve-lyrics must ask `advisory`, not the
    staging-only helper, or the production switch would be silently ignored."""
    from pathlib import Path
    root = Path(__file__).parents[1]
    assert "advisory as _language_staging_advisory" in (root / "main.py").read_text()
    assert "from language_review import advisory as _language_staging_advisory" in (root / "batch_campaigns.py").read_text()
