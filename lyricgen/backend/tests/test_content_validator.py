"""Tests for content_validator's tmpdir hygiene.

These cover the leak that could exhaust /tmp on long-running workers
when the Vision-API check failed for every frame in a video — pre-fix
the orphan dir under /tmp/genly_validate_* was never cleaned up
because cleanup keyed off frame_paths[0].
"""

import os
import tempfile
from unittest.mock import patch

import content_validator


def test_extract_frames_returns_tmp_dir_alongside_paths(tmp_path):
    # _extract_frames must always return both the (possibly-empty) frame
    # list and the tmp_dir so the caller can clean up unconditionally.
    fake_video = str(tmp_path / "fake.mp4")
    open(fake_video, "wb").close()

    with patch.object(content_validator.subprocess, "run") as mock_run:
        # ffprobe ok, every ffmpeg call fails.
        def _side_effect(cmd, **kwargs):
            class _R:
                stdout = "60.0"
                returncode = 0
            if cmd[0] == "ffprobe":
                return _R()
            # ffmpeg: simulate failure by writing nothing.
            class _F:
                stdout = ""
                returncode = 1
            return _F()
        mock_run.side_effect = _side_effect

        frame_paths, tmp_dir, planned_frames = content_validator._extract_frames(fake_video)

    assert frame_paths == []
    assert planned_frames > 0
    assert os.path.isdir(tmp_dir)
    # Caller is responsible for cleanup; verify it's actually removable.
    os.rmdir(tmp_dir)


def test_validate_video_cleans_tmpdir_when_no_frames_extracted(tmp_path):
    # End-to-end: when every ffmpeg frame extraction fails, the validator
    # must still remove the mkdtemp it created. Pre-fix this leaked one
    # /tmp/genly_validate_* per validation attempt.
    fake_video = str(tmp_path / "fake.mp4")
    open(fake_video, "wb").close()

    pre = set(os.listdir(tempfile.gettempdir()))

    with patch.object(content_validator.subprocess, "run") as mock_run:
        def _side_effect(cmd, **kwargs):
            class _R:
                stdout = "60.0"
                returncode = 0
            if cmd[0] == "ffprobe":
                return _R()
            class _F:
                stdout = ""
                returncode = 1
            return _F()
        mock_run.side_effect = _side_effect

        result = content_validator.validate_video(fake_video, job_id=None)

    # Validator fails-closed when zero frames could be checked.
    assert result["passed"] is False
    assert result["frames_checked"] == 0

    post = set(os.listdir(tempfile.gettempdir()))
    new_dirs = post - pre
    leftover = [d for d in new_dirs if d.startswith("genly_validate_")]
    assert leftover == [], f"Tmpdir leak: {leftover}"


def test_validate_video_fails_closed_on_partial_check_error(tmp_path, monkeypatch):
    tmp_dir = tempfile.mkdtemp(prefix="genly_validate_test_")
    frames = []
    for i in range(2):
        frame = os.path.join(tmp_dir, f"frame_{i}.jpg")
        open(frame, "wb").close()
        frames.append(frame)
    monkeypatch.setattr(content_validator, "_extract_frames", lambda _p: (frames, tmp_dir, 2))
    calls = {"n": 0}

    def _check(_path):
        calls["n"] += 1
        if calls["n"] == 2:
            raise content_validator.ValidatorCheckError("timeout")
        return {"safe": True, "issues": []}

    monkeypatch.setattr(content_validator, "_check_frame_with_gemini", _check)
    result = content_validator.validate_video("fake.mp4")
    assert result["passed"] is False
    assert result["frames_checked"] == 1
    assert result["check_errors"] == 1


def test_extract_frames_samples_entire_long_video(tmp_path):
    fake_video = str(tmp_path / "long.mp4")
    open(fake_video, "wb").close()
    timestamps = []

    with patch.object(content_validator.subprocess, "run") as mock_run:
        def _side_effect(cmd, **kwargs):
            class _R:
                stdout = "311.0"
                returncode = 0
            if cmd[0] == "ffmpeg":
                timestamps.append(float(cmd[cmd.index("-ss") + 1]))
            return _R()
        mock_run.side_effect = _side_effect
        _, tmp_dir, planned_frames = content_validator._extract_frames(fake_video)

    assert len(timestamps) == 48
    assert planned_frames == 48
    assert timestamps[0] <= 1.0
    assert timestamps[-1] >= 300.0
    os.rmdir(tmp_dir)


def test_validate_video_fails_closed_on_partial_extraction(tmp_path, monkeypatch):
    tmp_dir = tempfile.mkdtemp(prefix="genly_validate_test_")
    frame = os.path.join(tmp_dir, "frame_0.jpg")
    open(frame, "wb").close()
    monkeypatch.setattr(
        content_validator, "_extract_frames", lambda _p: ([frame], tmp_dir, 48)
    )
    monkeypatch.setattr(
        content_validator, "_check_frame_with_gemini",
        lambda _p: {"safe": True, "issues": []},
    )
    result = content_validator.validate_video("fake.mp4")
    assert result["passed"] is False
    assert result["extraction_errors"] == 47


def test_extract_frames_unknown_duration_fails_closed(tmp_path):
    fake_video = str(tmp_path / "unknown.mp4")
    open(fake_video, "wb").close()
    with patch.object(content_validator.subprocess, "run", side_effect=TimeoutError("ffprobe")):
        frames, tmp_dir, planned = content_validator._extract_frames(fake_video)
    assert frames == []
    assert planned == 1
    os.rmdir(tmp_dir)


def _classified(*, people=False, atmospherics=False, brand=False):
    detections = {
        "people": people,
        "atmospherics": atmospherics,
        "brand": brand,
    }
    issues = [
        {"category": category, "reason": f"visible {category}"}
        for category, present in detections.items() if present
    ]
    return {"safe": not people and not brand, "detections": detections, "issues": issues}


def test_people_opt_in_does_not_bypass_brand_gate(tmp_path, monkeypatch):
    image = tmp_path / "frame.jpg"
    image.write_bytes(b"jpg")
    monkeypatch.setattr(
        content_validator,
        "_check_frame_with_gemini",
        lambda _path: _classified(people=True, brand=True),
    )

    result = content_validator.validate_image(str(image), allow_people=True)

    assert result["passed"] is False
    assert not any("people:" in issue["type"] for issue in result["issues"])
    assert any("brand:" in issue["type"] for issue in result["issues"])


def test_atmospherics_shadow_is_observed_without_blocking(tmp_path, monkeypatch):
    image = tmp_path / "frame.jpg"
    image.write_bytes(b"jpg")
    monkeypatch.setattr(
        content_validator,
        "_check_frame_with_gemini",
        lambda _path: _classified(atmospherics=True),
    )

    result = content_validator.validate_image(
        str(image),
        allow_atmospherics=False,
        observe_atmospherics=True,
        enforce_atmospherics=False,
    )

    assert result["passed"] is True
    assert result["issues"] == []
    assert result["shadow_atmospherics_detected"] is True
    assert result["observations"]


def test_atmospherics_enforce_blocks_without_operator_opt_in(tmp_path, monkeypatch):
    image = tmp_path / "frame.jpg"
    image.write_bytes(b"jpg")
    monkeypatch.setattr(
        content_validator,
        "_check_frame_with_gemini",
        lambda _path: _classified(atmospherics=True),
    )

    result = content_validator.validate_image(
        str(image),
        allow_atmospherics=False,
        observe_atmospherics=False,
        enforce_atmospherics=True,
    )

    assert result["passed"] is False
    assert any("atmospherics:" in issue["type"] for issue in result["issues"])


def test_atmospherics_opt_in_does_not_bypass_people_gate(tmp_path, monkeypatch):
    image = tmp_path / "frame.jpg"
    image.write_bytes(b"jpg")
    monkeypatch.setattr(
        content_validator,
        "_check_frame_with_gemini",
        lambda _path: _classified(people=True, atmospherics=True),
    )

    result = content_validator.validate_image(
        str(image),
        allow_people=False,
        allow_atmospherics=True,
        enforce_atmospherics=False,
    )

    assert result["passed"] is False
    assert any("people:" in issue["type"] for issue in result["issues"])


def test_categorized_issue_promotes_contradictory_false_detection():
    result = content_validator._evaluate_frame_result(
        {
            "safe": True,
            "detections": {
                "people": False,
                "atmospherics": False,
                "brand": False,
            },
            "issues": [{"category": "people", "reason": "visible hand"}],
        },
        allow_people=False,
        allow_atmospherics=False,
        enforce_atmospherics=True,
    )

    assert result["passed"] is False
    assert result["detections"]["people"] is True
    assert any("visible hand" in issue for issue in result["issues"])


def test_legacy_safe_with_unattributed_issues_fails_closed():
    result = content_validator._evaluate_frame_result(
        {"safe": True, "issues": ["visible human"]},
        allow_people=True,
        allow_atmospherics=True,
        enforce_atmospherics=False,
    )

    assert result["passed"] is False
