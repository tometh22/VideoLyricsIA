"""The "Cine" frame format: a deterministic 2.39:1 letterbox applied to the
finished 16:9 master (opposite of the stochastic Veo bars stripped by
_strip_letterbox). Runs REAL ffmpeg on a tiny clip; skips where ffmpeg is
absent, like test_ass_integration.
"""
import shutil
import subprocess

import pytest

import pipeline

_FFMPEG = shutil.which("ffmpeg")
_FFPROBE = shutil.which("ffprobe")

pytestmark = pytest.mark.skipif(
    not (_FFMPEG and _FFPROBE), reason="ffmpeg/ffprobe not available"
)


def _make_1080(path: str):
    subprocess.run(
        [_FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "testsrc=size=1920x1080:duration=2:rate=15",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-t", "2", path],
        check=True, timeout=120,
    )


def _content_region(path: str):
    """cropdetect's non-black content box → (w,h,x,y) or None."""
    import re
    out = subprocess.run(
        [_FFMPEG, "-hide_banner", "-nostats", "-i", path,
         "-vf", "cropdetect=limit=24:round=2:reset=0", "-f", "null", "-"],
        capture_output=True, text=True, timeout=60,
    )
    m = re.findall(r"crop=(\d+):(\d+):(-?\d+):(-?\d+)", out.stderr or "")
    return tuple(int(v) for v in m[-1]) if m else None


def test_cine_adds_symmetric_239_letterbox(tmp_path):
    clip = str(tmp_path / "v.mp4")
    _make_1080(clip)
    assert pipeline._video_dims(clip) == (1920, 1080)

    assert pipeline._apply_frame_format(clip, "cine") is True

    # Frame stays 1920x1080; content is now a centered 2.39:1 band.
    assert pipeline._video_dims(clip) == (1920, 1080)
    region = _content_region(clip)
    assert region is not None
    w, h, x, y = region
    assert w == 1920 and x == 0
    # content height ≈ 1920/2.39 ≈ 802 (even), symmetric bars ≈ 139 each
    assert 796 <= h <= 808
    top_bar = y
    bottom_bar = 1080 - h - y
    assert abs(top_bar - bottom_bar) <= 2          # symmetric
    assert 130 <= top_bar <= 145                   # real bars, ~139px


def test_full_is_a_noop(tmp_path):
    clip = str(tmp_path / "v.mp4")
    _make_1080(clip)
    import os
    before = os.path.getsize(clip)
    assert pipeline._apply_frame_format(clip, "full") is False
    assert os.path.getsize(clip) == before        # untouched, not re-encoded
    assert _content_region(clip) == (1920, 1080, 0, 0)  # no bars


def test_unknown_format_is_a_noop(tmp_path):
    clip = str(tmp_path / "v.mp4")
    _make_1080(clip)
    assert pipeline._apply_frame_format(clip, "") is False
    assert pipeline._apply_frame_format(clip, "widescreen") is False
