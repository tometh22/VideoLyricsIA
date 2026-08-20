"""A lyrics/typography/metadata edit must never lose the background because
the validator ran out of Vertex quota.

Incident 2026-08-20 (UMG Chile, "La Funa", job 2861e17a44d4): the customer
edited two commas in the lyrics. run_edit_pipeline re-validated the SAME cached
Veo background it had already certified at 8/8 frames an hour earlier, one
frame came back `429 RESOURCE_EXHAUSTED`, and the fail-closed policy turned
that missing verdict into a rejection. The recovery branch then rendered the
video over the safe gradient AND recached the gradient over bg_r2_key_cached —
so the background was gone for good, with `issues: []` as the only evidence.

Two layers are pinned here:
  1. content_validator retries transient Vision errors before declaring an
     incomplete verdict, and labels the difference (`inconclusive`) between
     "never answered" and "saw a violation";
  2. run_edit_pipeline keeps a previously certified background when a
     foreground-only edit gets an inconclusive re-check — and still destroys it
     on a decisive policy finding.
"""
import inspect
import os
import tempfile

import pytest

import content_validator
import pipeline


def _classified(people=False, atmospherics=False, brand=False, issues=None):
    return {
        "safe": not people and not brand,
        "detections": {
            "people": people, "atmospherics": atmospherics, "brand": brand,
        },
        "issues": issues or [],
    }


# ---------------------------------------------------------------- validator


def test_quota_error_is_retried_before_failing_closed(monkeypatch):
    attempts = {"n": 0}

    class _Resp:
        text = '{"detections":{"people":false,"atmospherics":false,"brand":false},"issues":[]}'

    def _call_with_timeout(fn, timeout_s=None, label=None):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError(
                "429 RESOURCE_EXHAUSTED. {'error': {'code': 429}}"
            )
        return _Resp()

    monkeypatch.setattr(pipeline, "_call_with_timeout", _call_with_timeout)
    monkeypatch.setattr(pipeline, "_get_genai_client", lambda: object())
    monkeypatch.setattr(content_validator.time, "sleep", lambda _s: None)

    with tempfile.NamedTemporaryFile(suffix=".jpg") as frame:
        frame.write(b"x")
        frame.flush()
        result = content_validator._check_frame_with_gemini(frame.name)

    assert attempts["n"] == 3, "a 429 must be retried, not treated as a verdict"
    assert result["safe"] is True


def test_non_retryable_error_is_not_retried(monkeypatch):
    attempts = {"n": 0}

    def _call_with_timeout(fn, timeout_s=None, label=None):
        attempts["n"] += 1
        raise RuntimeError("403 PERMISSION_DENIED")

    monkeypatch.setattr(pipeline, "_call_with_timeout", _call_with_timeout)
    monkeypatch.setattr(pipeline, "_get_genai_client", lambda: object())
    monkeypatch.setattr(content_validator.time, "sleep", lambda _s: None)

    with tempfile.NamedTemporaryFile(suffix=".jpg") as frame:
        frame.write(b"x")
        frame.flush()
        with pytest.raises(content_validator.ValidatorCheckError):
            content_validator._check_frame_with_gemini(frame.name)

    assert attempts["n"] == 1


@pytest.mark.parametrize("message,retryable", [
    ("429 RESOURCE_EXHAUSTED", True),
    ("503 UNAVAILABLE", True),
    ("VALIDATOR call timed out after 45s", True),
    ("403 PERMISSION_DENIED", False),
    ("Gemini Vision 'detections' must be a JSON object", False),
])
def test_retryable_error_classification(message, retryable):
    assert content_validator._is_retryable_vision_error(
        RuntimeError(message)
    ) is retryable


def test_incomplete_scan_is_inconclusive_but_still_fails(monkeypatch):
    tmp_dir = tempfile.mkdtemp(prefix="genly_validate_test_")
    frames = []
    for i in range(2):
        frame = os.path.join(tmp_dir, f"frame_{i}.jpg")
        open(frame, "wb").close()
        frames.append(frame)
    monkeypatch.setattr(
        content_validator, "_extract_frames", lambda _p: (frames, tmp_dir, 2)
    )
    calls = {"n": 0}

    def _check(_path):
        calls["n"] += 1
        if calls["n"] == 2:
            raise content_validator.ValidatorCheckError("429 RESOURCE_EXHAUSTED")
        return _classified()

    monkeypatch.setattr(content_validator, "_check_frame_with_gemini", _check)
    result = content_validator.validate_video("fake.mp4")

    assert result["passed"] is False, "fail-closed contract is unchanged"
    assert result["inconclusive"] is True


def test_policy_violation_is_not_inconclusive(monkeypatch):
    tmp_dir = tempfile.mkdtemp(prefix="genly_validate_test_")
    frame = os.path.join(tmp_dir, "frame_0.jpg")
    open(frame, "wb").close()
    monkeypatch.setattr(
        content_validator, "_extract_frames", lambda _p: ([frame], tmp_dir, 1)
    )
    monkeypatch.setattr(
        content_validator,
        "_check_frame_with_gemini",
        lambda _p: _classified(people=True),
    )

    result = content_validator.validate_video("fake.mp4")

    assert result["passed"] is False
    assert result["inconclusive"] is False, (
        "a decisive finding IS evidence about the asset — it must keep "
        "destroying the background"
    )


def test_clean_scan_is_not_inconclusive(monkeypatch):
    tmp_dir = tempfile.mkdtemp(prefix="genly_validate_test_")
    frame = os.path.join(tmp_dir, "frame_0.jpg")
    open(frame, "wb").close()
    monkeypatch.setattr(
        content_validator, "_extract_frames", lambda _p: ([frame], tmp_dir, 1)
    )
    monkeypatch.setattr(
        content_validator, "_check_frame_with_gemini", lambda _p: _classified()
    )

    result = content_validator.validate_video("fake.mp4")

    assert result["passed"] is True
    assert result["inconclusive"] is False


# ------------------------------------------------------------- keep decision


CERTIFIED = {"passed": True, "issues": [], "policy_mode": "enforce"}


def test_inconclusive_recheck_keeps_reused_background(tmp_path):
    asset = tmp_path / "bg_cached_edit.mp4"
    asset.write_bytes(b"veo")
    for edit_type in ("lyrics", "typography", "metadata"):
        assert pipeline._should_keep_certified_background(
            edit_type,
            verdict={"passed": False, "inconclusive": True},
            prior_validation_result=CERTIFIED,
            asset_path=str(asset),
        ) is True, f"{edit_type} edit must not discard the certified background"


def test_decisive_violation_still_destroys_the_background(tmp_path):
    asset = tmp_path / "bg_cached_edit.mp4"
    asset.write_bytes(b"veo")
    assert pipeline._should_keep_certified_background(
        "lyrics",
        verdict={
            "passed": False,
            "inconclusive": False,
            "issues": [{"frame": 0, "type": "people: recognizable face"}],
        },
        prior_validation_result=CERTIFIED,
        asset_path=str(asset),
    ) is False


def test_background_changing_edits_are_not_covered(tmp_path):
    asset = tmp_path / "bg.mp4"
    asset.write_bytes(b"veo")
    for edit_type in ("background", "scene", "background_library", "custom"):
        assert pipeline._should_keep_certified_background(
            edit_type,
            verdict={"passed": False, "inconclusive": True},
            prior_validation_result=CERTIFIED,
            asset_path=str(asset),
        ) is False, f"{edit_type} introduces a NEW asset — nothing was certified"


def test_uncertified_background_is_not_kept(tmp_path):
    asset = tmp_path / "bg.mp4"
    asset.write_bytes(b"veo")
    for prior in ({}, {"passed": False}, {"passed": None}):
        assert pipeline._should_keep_certified_background(
            "lyrics",
            verdict={"passed": False, "inconclusive": True},
            prior_validation_result=prior,
            asset_path=str(asset),
        ) is False


def test_missing_asset_or_forced_fallback_is_not_kept(tmp_path):
    asset = tmp_path / "bg.mp4"
    asset.write_bytes(b"veo")
    assert pipeline._should_keep_certified_background(
        "lyrics",
        verdict={"passed": False, "inconclusive": True},
        prior_validation_result=CERTIFIED,
        asset_path=str(tmp_path / "missing.mp4"),
    ) is False
    assert pipeline._should_keep_certified_background(
        "lyrics",
        verdict={"passed": False, "inconclusive": True},
        prior_validation_result=CERTIFIED,
        asset_path=str(asset),
        forced_fallback=True,
    ) is False


def test_validator_exposes_the_verdict_to_the_edit_path(monkeypatch, tmp_path):
    asset = tmp_path / "bg.mp4"
    asset.write_bytes(b"veo")
    monkeypatch.setattr(pipeline, "_background_safety_policy", lambda *a, **k: {
        "should_validate": True, "allow_people": False,
        "allow_atmospherics": False, "observe_atmospherics": False,
        "validate_atmospherics": True, "reason": "universal_mandatory",
        "tenant_id": "universal_chile", "billing_group": "umg",
        "policy_version": "background-v5", "policy_mode": "enforce",
        "is_umg": True, "atmospherics_policy": {},
    })
    monkeypatch.setattr(pipeline, "update_job", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_validation_observe_only", lambda: False)
    monkeypatch.setattr(
        content_validator, "validate_video",
        lambda *a, **k: {"passed": False, "inconclusive": True, "issues": []},
    )

    verdict = {}
    ok = pipeline._validate_background_asset_for_job(
        "job1", str(asset), "prompt", failure_status=None, verdict_out=verdict,
    )

    assert ok is False
    assert verdict["inconclusive"] is True


def test_edit_pipeline_wires_the_keep_branch():
    """The helper is only useful if run_edit_pipeline consults it BEFORE the
    gradient-recovery branch that overwrites bg_r2_key_cached."""
    src = inspect.getsource(pipeline.run_edit_pipeline)
    assert "verdict_out=_edit_verdict" in src
    assert "_should_keep_certified_background(" in src
    keep_at = src.index("_should_keep_certified_background(")
    recover_at = src.index("_recover_edit_background = bool(")
    assert keep_at < recover_at, (
        "the keep decision must run before the gradient fallback claims the "
        "background"
    )
    assert "_prior_validation_result" in src, (
        "the keep decision needs the pre-edit verdict, snapshotted before this "
        "edit overwrites validation_result"
    )
