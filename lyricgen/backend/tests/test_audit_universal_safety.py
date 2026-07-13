"""Classification tests for the read-only Universal retrospective audit."""

from types import SimpleNamespace

from background_attestation import (
    BACKGROUND_ATTESTATION_VERSION,
    DELIVERY_ATTESTATION_VERSION,
)
from background_policy import POLICY_VERSION
from scripts.audit_universal_safety import build_report, is_universal_account


def _row(*, tenant="universal_argentina", group="universal_music", **updates):
    validation = updates.pop("validation_result", {})
    job = SimpleNamespace(
        job_id=updates.pop("job_id", "abc123"),
        created_at=None,
        tenant_id=tenant,
        status=updates.pop("status", "pending_review"),
        progress=updates.pop("progress", 100),
        error=updates.pop("error", None),
        validation_result=validation,
        **updates,
    )
    user = SimpleNamespace(billing_group=group)
    return job, user


def _current_validation():
    return {
        "passed": True,
        "allow_people": False,
        "policy_version": POLICY_VERSION,
        "validation_scope": "sampled_asset",
        "attestation": {
            "version": BACKGROUND_ATTESTATION_VERSION,
            "policy_version": POLICY_VERSION,
            "is_universal": True,
            "allow_people": False,
            "asset_sha256": "a" * 64,
            "validation_sha256": "b" * 64,
        },
        "delivery_attestation": {
            "version": DELIVERY_ATTESTATION_VERSION,
            "policy_version": POLICY_VERSION,
            "background_sha256": "a" * 64,
            "deliverables": {"video_url": {"sha256": "c" * 64}},
        },
    }


def test_account_match_covers_tenant_and_billing_group():
    assert is_universal_account("universal_chile", None)
    assert is_universal_account("country_team", " Universal-Music ")
    assert not is_universal_account("genly", None)


def test_current_visible_delivery_is_attested():
    report = build_report([_row(validation_result=_current_validation())])
    assert report["categories"] == {"current_attested_delivery": 1}
    assert report["requires_attention"] == []


def test_visible_v5_job_is_not_silently_upgraded():
    report = build_report([_row(validation_result={
        "passed": True,
        "allow_people": False,
        "policy_version": "background-v5",
    })])
    assert report["categories"] == {
        "visible_legacy_evidence_needs_revalidation": 1,
    }
    assert len(report["requires_attention"]) == 1


def test_legacy_validation_failure_is_reported_for_recovery_review():
    report = build_report([_row(
        status="validation_failed",
        validation_result={
            "passed": False,
            "allow_people": False,
            "policy_version": "background-v5",
        },
    )])
    assert report["categories"] == {"blocked_by_legacy_validator": 1}


def test_non_universal_jobs_are_excluded():
    report = build_report([_row(tenant="genly", group=None)])
    assert report["universal_jobs"] == 0


def test_deep_local_scan_is_explicit_and_read_only_by_construction():
    import inspect
    from scripts.audit_universal_safety import deep_local_scan

    source = inspect.getsource(deep_local_scan)
    assert "download_object" in source
    assert "_check_frame_with_local_detector" in source
    assert "update_job" not in source
    assert "commit(" not in source
