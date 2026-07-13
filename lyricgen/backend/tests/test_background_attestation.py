"""Adversarial tests for Universal byte-bound publication evidence."""

import pytest

from background_attestation import (
    AttestationError,
    build_background_attestation,
    build_delivery_attestation,
    verify_background_attestation,
)
from background_policy import POLICY_VERSION
from content_validator import GEMINI_VALIDATOR_VERSION, LOCAL_VALIDATOR_VERSION


def _policy():
    return {
        "is_umg": True,
        "allow_people": False,
        "policy_version": POLICY_VERSION,
        "policy_mode": "enforce",
        "tenant_id": "universal_argentina",
        "billing_group": "universal_music",
    }


def _dual_validation():
    return {
        "passed": True,
        "issues": [],
        "allow_people": False,
        "policy_version": POLICY_VERSION,
        "validation_scope": "sampled_asset",
        "frames_checked": 8,
        "frames_planned": 8,
        "extraction_errors": 0,
        "check_errors": 0,
        "secondary_required": True,
        "secondary_frames_checked": 32,
        "secondary_frames_planned": 32,
        "secondary_extraction_errors": 0,
        "secondary_errors": 0,
        "validator_stack": [GEMINI_VALIDATOR_VERSION, LOCAL_VALIDATOR_VERSION],
    }


def test_current_dual_validation_seals_and_verifies_exact_bytes(tmp_path):
    asset = tmp_path / "background.mp4"
    asset.write_bytes(b"safe-background")
    validation = _dual_validation()
    seal = build_background_attestation(str(asset), validation, _policy())

    verify_background_attestation(
        str(asset), seal, _policy(), validation=validation
    )

    assert len(seal["asset_sha256"]) == 64
    assert len(seal["validation_sha256"]) == 64


def test_missing_independent_detector_cannot_be_attested(tmp_path):
    asset = tmp_path / "background.mp4"
    asset.write_bytes(b"candidate")
    validation = _dual_validation()
    validation["validator_stack"] = [GEMINI_VALIDATOR_VERSION]

    with pytest.raises(AttestationError, match="independent detector"):
        build_background_attestation(str(asset), validation, _policy())


def test_incomplete_dense_coverage_cannot_be_attested(tmp_path):
    asset = tmp_path / "background.mp4"
    asset.write_bytes(b"candidate")
    validation = _dual_validation()
    validation["secondary_frames_checked"] = 31

    with pytest.raises(AttestationError, match="every dense sample"):
        build_background_attestation(str(asset), validation, _policy())


def test_asset_mutation_after_validation_is_blocked(tmp_path):
    asset = tmp_path / "background.mp4"
    asset.write_bytes(b"safe-background")
    validation = _dual_validation()
    seal = build_background_attestation(str(asset), validation, _policy())
    asset.write_bytes(b"different-background")

    with pytest.raises(AttestationError, match="bytes differ"):
        verify_background_attestation(
            str(asset), seal, _policy(), validation=validation
        )


def test_evidence_mutation_after_sealing_is_blocked(tmp_path):
    asset = tmp_path / "background.mp4"
    asset.write_bytes(b"safe-background")
    validation = _dual_validation()
    seal = build_background_attestation(str(asset), validation, _policy())
    validation["frames_checked"] = 1

    with pytest.raises(AttestationError, match="evidence changed"):
        verify_background_attestation(
            str(asset), seal, _policy(), validation=validation
        )


def test_renderer_owned_fallback_needs_no_external_model(tmp_path):
    asset = tmp_path / "gradient.mp4"
    asset.write_bytes(b"renderer-owned-gradient")
    validation = {
        "passed": True,
        "allow_people": False,
        "policy_version": POLICY_VERSION,
        "validation_scope": "deterministic_local_fallback",
        "validator_stack": [],
    }

    seal = build_background_attestation(str(asset), validation, _policy())
    verify_background_attestation(
        str(asset), seal, _policy(), validation=validation
    )


def test_scene_scope_requires_every_unique_scene_to_be_current(tmp_path):
    asset = tmp_path / "scenes.mp4"
    asset.write_bytes(b"stitched-scenes")
    validation = _dual_validation()
    validation.update({
        "validation_scope": "each_unique_scene_clip",
        "scene_validations_complete": False,
    })

    with pytest.raises(AttestationError, match="every unique scene"):
        build_background_attestation(str(asset), validation, _policy())


def test_delivery_attestation_hashes_every_local_critical_file(tmp_path):
    files = {
        "video_url": "/download/job/video",
        "short_url": "/download/job/short",
        "thumbnail_url": "/download/job/thumbnail",
        "umg_master_url": "/download/job/umg_master",
    }
    for filename in ("lyric_video.mp4", "short.mp4", "thumbnail.jpg"):
        (tmp_path / filename).write_bytes(filename.encode())

    delivery = build_delivery_attestation(
        job_dir=str(tmp_path),
        files=files,
        background_attestation={
            "asset_sha256": "a" * 64,
            "policy_version": POLICY_VERSION,
        },
    )

    assert set(delivery["deliverables"]) == {
        "video_url", "short_url", "thumbnail_url",
    }
    assert all(
        len(item["sha256"]) == 64
        for item in delivery["deliverables"].values()
    )
