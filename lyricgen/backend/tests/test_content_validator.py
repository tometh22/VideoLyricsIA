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

        frame_paths, tmp_dir = content_validator._extract_frames(fake_video)

    assert frame_paths == []
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
