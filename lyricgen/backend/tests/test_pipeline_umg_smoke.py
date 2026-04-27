"""End-to-end smoke test for the UMG render path.

Generates a tiny audio fixture + uses an existing library image background,
calls `generate_lyric_video` with a UMG spec, asserts the output ProRes .mov
exists and passes `_validate_umg_master`.

Slow (real ProRes encode, ~10-30 s). Skipped in CI by default. Run manually
before each UMG-delivery week:

    pytest -m umg_smoke

Or specifically this module:

    pytest tests/test_pipeline_umg_smoke.py -m umg_smoke -v

Requires: ffmpeg + ImageMagick on PATH, moviepy installed in the venv,
fonts in lyricgen/assets/fonts, at least one library background image.
"""

import os
import shutil
import subprocess

import pytest

pytestmark = [
    pytest.mark.umg_smoke,
    pytest.mark.skipif(
        shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
        reason="ffmpeg/ffprobe not available",
    ),
]


def _make_test_image(out_path: str, width: int = 1920, height: int = 1080) -> None:
    """Generate a small gradient image fixture via ffmpeg (so the smoke test
    doesn't depend on the assets/backgrounds/library/ contents, which may be
    .gitignored placeholders during dev)."""
    cmd = [
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi",
        "-i", f"gradients=s={width}x{height}:c0=blue:c1=red:duration=1",
        "-frames:v", "1",
        out_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 0, f"ffmpeg image fixture failed: {result.stderr}"


def _make_test_mp3(out_path: str, duration: float = 5.0) -> None:
    """Generate a short silent MP3 fixture via ffmpeg."""
    cmd = [
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo",
        "-t", str(duration),
        "-c:a", "libmp3lame", "-b:a", "128k",
        out_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 0, f"ffmpeg fixture failed: {result.stderr}"


def test_umg_render_smoke_hd_24fps(tmp_path):
    """Real ProRes 422 HQ render at 1920x1080 / 24fps from a library image
    background and silent MP3. Validates the output passes the spec check."""

    bg = str(tmp_path / "bg.jpg")
    _make_test_image(bg)

    # Tiny MP3 fixture
    mp3 = str(tmp_path / "smoke.mp3")
    _make_test_mp3(mp3, duration=5.0)

    # Tiny segment list — covers the full audio with one lyric line
    segments = [{"start": 1.0, "end": 4.0, "text": "Smoke test lyric"}]

    job_dir = str(tmp_path / "job")
    os.makedirs(job_dir, exist_ok=True)

    # Import inside the test so pytest can collect even if dependencies are
    # transiently broken at import time (we want a clear skip, not a collection
    # error).
    from pipeline import _validate_umg_master, generate_lyric_video
    from render_spec import RenderSpec

    spec = RenderSpec.umg(frame_size="HD", fps=24.0, prores_profile=3)
    out_path, _font, _bg_source = generate_lyric_video(
        mp3_path=mp3,
        segments=segments,
        style="cinematic",
        job_dir=job_dir,
        artist="Test Artist",
        bg_image_path=bg,
        spec=spec,
    )

    assert os.path.exists(out_path), f"output not created: {out_path}"
    assert out_path.endswith("umg_master.mov")

    errors = _validate_umg_master(out_path, spec)
    assert errors == [], f"UMG validation failed: {errors}"


def test_umg_render_smoke_fractional_fps(tmp_path):
    """Render at 23.976 fps to verify the R1 fix produces correct rational
    timebase that the validator accepts."""

    bg = str(tmp_path / "bg.jpg")
    _make_test_image(bg)

    mp3 = str(tmp_path / "smoke_2398.mp3")
    _make_test_mp3(mp3, duration=4.0)
    segments = [{"start": 0.5, "end": 3.5, "text": "Fractional fps test"}]

    job_dir = str(tmp_path / "job_2398")
    os.makedirs(job_dir, exist_ok=True)

    from pipeline import _validate_umg_master, generate_lyric_video
    from render_spec import RenderSpec

    spec = RenderSpec.umg(frame_size="HD", fps=23.976, prores_profile=3)
    out_path, _font, _bg_source = generate_lyric_video(
        mp3_path=mp3,
        segments=segments,
        style="cinematic",
        job_dir=job_dir,
        artist="Test Artist",
        bg_image_path=bg,
        spec=spec,
    )

    assert os.path.exists(out_path)
    errors = _validate_umg_master(out_path, spec)
    assert errors == [], (
        f"UMG validation failed for fractional fps: {errors}. "
        f"This likely means the R1 rational-fps fix isn't taking effect."
    )
