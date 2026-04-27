"""Tests for `_validate_umg_master` — the post-render gate that prevents
non-compliant ProRes masters from reaching UMG.

Strategy: generate a known-good ProRes file via direct ffmpeg, assert the
validator returns []. Mutate one field at a time (codec, dimensions, fps,
pix_fmt, container) and assert each violation is caught.

ffmpeg is required. Tests skip if ffmpeg is not on PATH.
"""

import os
import shutil
import subprocess

import pytest

from pipeline import _validate_umg_master
from render_spec import RenderSpec


pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not available",
)


def _make_prores(out_path: str, *,
                 width: int = 1920, height: int = 1080,
                 fps: str = "24",
                 prores_profile: int = 3,
                 pix_fmt: str = "yuv422p10le",
                 colorspace: str = "bt709",
                 dar: str = "16:9",
                 duration: float = 1.0) -> None:
    """Encode a tiny ProRes test fixture via direct ffmpeg."""
    cmd = [
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:r={fps}",
        "-t", str(duration),
        "-r", fps,
        "-c:v", "prores_ks",
        "-profile:v", str(prores_profile),
        "-pix_fmt", pix_fmt,
        "-vendor", "apl0",
        "-color_primaries", colorspace,
        "-color_trc", colorspace,
        "-colorspace", colorspace,
        "-color_range", "tv",
        "-aspect", dar,
        "-vf", "setsar=1",
        out_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 0, f"ffmpeg failed: {result.stderr}"


def _make_h264_mp4(out_path: str, *,
                   width: int = 1920, height: int = 1080,
                   fps: str = "24", duration: float = 1.0) -> None:
    """Encode a tiny H.264 MP4 (the wrong codec/container for UMG)."""
    cmd = [
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:r={fps}",
        "-t", str(duration),
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        out_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 0, f"ffmpeg failed: {result.stderr}"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_valid_umg_hd_master_passes(tmp_path):
    """A correctly-encoded HD ProRes 422 HQ master should produce no errors."""
    fixture = str(tmp_path / "valid_master.mov")
    _make_prores(fixture)

    spec = RenderSpec.umg(frame_size="HD", fps=24.0, prores_profile=3)
    errors = _validate_umg_master(fixture, spec)
    assert errors == [], f"unexpected errors: {errors}"


def test_valid_umg_dci_2k_master_passes(tmp_path):
    """DCI-2K (2048x1080, 256:135 DAR) should also pass."""
    fixture = str(tmp_path / "valid_dci2k.mov")
    _make_prores(fixture, width=2048, height=1080, dar="256:135")

    spec = RenderSpec.umg(frame_size="DCI-2K", fps=24.0, prores_profile=3)
    errors = _validate_umg_master(fixture, spec)
    assert errors == [], f"unexpected errors: {errors}"


# ---------------------------------------------------------------------------
# Mutations — each should be caught
# ---------------------------------------------------------------------------


def test_wrong_codec_caught(tmp_path):
    """An H.264 MP4 should fail codec, profile, and pix_fmt checks. (ffmpeg
    reports MP4 and MOV under the same format_name string, so format_name
    alone doesn't distinguish them — the codec/profile errors are what
    actually catch a wrong-codec render.)"""
    fixture = str(tmp_path / "wrong_codec.mp4")
    _make_h264_mp4(fixture)

    spec = RenderSpec.umg(frame_size="HD", fps=24.0, prores_profile=3)
    errors = _validate_umg_master(fixture, spec)
    assert any("codec_name" in e for e in errors), errors
    assert any("pix_fmt" in e for e in errors), errors


def test_wrong_dimensions_caught(tmp_path):
    """A ProRes file with wrong dimensions should be caught."""
    fixture = str(tmp_path / "wrong_dims.mov")
    _make_prores(fixture, width=1280, height=720)

    spec = RenderSpec.umg(frame_size="HD", fps=24.0, prores_profile=3)
    errors = _validate_umg_master(fixture, spec)
    assert any("dimensions" in e for e in errors), errors


def test_wrong_fps_caught(tmp_path):
    """An integer fps that doesn't match spec should be caught."""
    fixture = str(tmp_path / "wrong_fps.mov")
    _make_prores(fixture, fps="30")

    spec = RenderSpec.umg(frame_size="HD", fps=24.0, prores_profile=3)
    errors = _validate_umg_master(fixture, spec)
    assert any("r_frame_rate" in e for e in errors), errors


def test_decimal_fps_for_fractional_spec_caught(tmp_path):
    """If spec is 23.976 (rational 24000/1001), output must be exactly that
    rational. Decimal 24/1 should fail (R1 fix)."""
    fixture = str(tmp_path / "wrong_rational.mov")
    _make_prores(fixture, fps="24")  # encoded as 24/1, not 24000/1001

    spec = RenderSpec.umg(frame_size="HD", fps=23.976, prores_profile=3)
    errors = _validate_umg_master(fixture, spec)
    assert any("exact rational required" in e for e in errors), errors


def test_correct_rational_fps_passes(tmp_path):
    """If we encode at 24000/1001 explicitly, validator should accept it."""
    fixture = str(tmp_path / "correct_rational.mov")
    _make_prores(fixture, fps="24000/1001")

    spec = RenderSpec.umg(frame_size="HD", fps=23.976, prores_profile=3)
    errors = _validate_umg_master(fixture, spec)
    assert errors == [], f"unexpected errors: {errors}"


def test_wrong_pix_fmt_caught(tmp_path):
    """ProRes 422 HQ requires yuv422p10le; 4444 (yuv444p10le) is wrong."""
    fixture = str(tmp_path / "wrong_pix_fmt.mov")
    _make_prores(fixture, prores_profile=4, pix_fmt="yuv444p10le")

    # Spec asks for profile 3 (HQ), but file is profile 4 (4444)
    spec = RenderSpec.umg(frame_size="HD", fps=24.0, prores_profile=3)
    errors = _validate_umg_master(fixture, spec)
    assert any("profile" in e for e in errors), errors
    assert any("pix_fmt" in e for e in errors), errors


def test_wrong_dar_caught(tmp_path):
    """If file is 16:9 DAR but spec requires 256:135, should be caught."""
    fixture = str(tmp_path / "wrong_dar.mov")
    _make_prores(fixture, dar="16:9")

    # Build a custom spec claiming DCI-4K dims but mismatched encoded file
    # (we encode at 1920x1080 16:9, then assert against DCI-2K which expects 256:135)
    spec = RenderSpec.umg(frame_size="DCI-2K", fps=24.0, prores_profile=3)
    errors = _validate_umg_master(fixture, spec)
    # Either dimensions or DAR will mismatch — both are violations
    assert any("dimensions" in e or "display_aspect_ratio" in e for e in errors), errors


def test_missing_color_primaries_does_not_fail(tmp_path):
    """Regression guard: ffprobe doesn't surface color_primaries/transfer for
    ProRes output across all ffmpeg versions. The validator must tolerate
    None for these fields as long as color_space is bt709."""
    fixture = str(tmp_path / "valid_master.mov")
    _make_prores(fixture)

    spec = RenderSpec.umg(frame_size="HD", fps=24.0, prores_profile=3)
    errors = _validate_umg_master(fixture, spec)
    # No color-related errors should appear (color_space should be bt709;
    # color_primaries / color_transfer can be None and that's OK)
    color_errors = [e for e in errors if "color_" in e]
    assert color_errors == [], f"unexpected color errors: {color_errors}"


def test_ffprobe_failure_returns_clear_error(tmp_path):
    """If the input file doesn't exist, validator should return an error
    (not raise) with 'ffprobe failed' in the message."""
    spec = RenderSpec.umg(frame_size="HD", fps=24.0, prores_profile=3)
    errors = _validate_umg_master(str(tmp_path / "nonexistent.mov"), spec)
    assert any("ffprobe failed" in e for e in errors), errors
