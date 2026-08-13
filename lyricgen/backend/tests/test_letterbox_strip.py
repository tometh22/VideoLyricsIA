"""Output-QA: detection + stripping of a letterbox Veo bakes into a clip.

Runs REAL ffmpeg on tiny synthetic clips (skips cleanly where ffmpeg is
missing, like test_ass_integration). Pins the two-gate contract:
  - a PURE-BLACK symmetric letterbox is detected and removed (frame refilled);
  - dark-but-real footage (dim, non-black bands) is NEVER cropped;
  - a clean clip is left byte-for-byte untouched.

Origin: "Seguir Viviendo Sin Tu Amor"/Spinetta 2026-07-07 — Veo baked a 2.39:1
anamorphic letterbox into some scenes; the cover pipeline scaled the bars up
with the picture. The purity gate is what makes stripping safe on this song's
near-black scenes.
"""
import os
import shutil
import subprocess

import pytest

import pipeline

_FFMPEG = shutil.which("ffmpeg")
_FFPROBE = shutil.which("ffprobe")

pytestmark = pytest.mark.skipif(
    not (_FFMPEG and _FFPROBE), reason="ffmpeg/ffprobe not available"
)

W, H = 640, 360  # tiny 16:9 for speed


def _encode(vf_src: str, path: str, dur: float = 6.0):
    """Encode a 6s clip from a lavfi source expression to `path`."""
    subprocess.run(
        [_FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", f"{vf_src}:duration={dur}:rate=15",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-t", str(dur), path],
        check=True, timeout=120,
    )


def _letterboxed(path: str, bar: int = 48, color: str = "black"):
    """A 16:9 clip whose content is padded with `bar`px bands top+bottom."""
    src = f"testsrc=size={W}x{H - 2 * bar}"
    subprocess.run(
        [_FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi", "-i",
         f"{src}:duration=6:rate=15",
         "-vf", f"pad={W}:{H}:0:{bar}:{color}",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-t", "6", path],
        check=True, timeout=120,
    )


def test_pure_black_letterbox_is_stripped(tmp_path):
    clip = str(tmp_path / "bars.mp4")
    _letterboxed(clip, bar=48, color="black")
    assert pipeline._video_dims(clip) == (W, H)

    changed = pipeline._strip_letterbox(clip)
    assert changed is True

    # Dimensions preserved, and the result no longer has black bars.
    assert pipeline._video_dims(clip) == (W, H)
    assert pipeline._detect_letterbox_crop(clip) is None


def test_dark_but_nonblack_bands_are_NOT_stripped(tmp_path):
    """The safety test: bands dark enough for cropdetect to flag (luma ~15,
    below the limit=24) but NOT pure black must be preserved — this is the
    near-black footage case. Only the purity gate rejects it."""
    clip = str(tmp_path / "dark.mp4")
    # rgb5 → stored luma ~20: cropdetect(limit=24) STILL flags it, but the
    # purity gate (YAVG 20 > 18) rejects — real dark footage isn't pure black.
    _letterboxed(clip, bar=48, color="0x050505")
    # geometry gate alone would propose a crop…
    assert pipeline._detect_letterbox_crop(clip) is not None
    # …but the full strip must refuse (not pure black).
    assert pipeline._strip_letterbox(clip) is False
    assert pipeline._video_dims(clip) == (W, H)


def test_clean_clip_is_untouched(tmp_path):
    clip = str(tmp_path / "clean.mp4")
    _encode(f"testsrc=size={W}x{H}", clip)
    before = os.path.getsize(clip)

    assert pipeline._detect_letterbox_crop(clip) is None
    assert pipeline._strip_letterbox(clip) is False
    assert os.path.getsize(clip) == before  # not re-encoded


def test_asymmetric_dark_edge_is_NOT_stripped(tmp_path):
    """A bright-top / dark-bottom scene (only ONE dark edge) is real content,
    not a letterbox — the symmetry gate must reject it."""
    clip = str(tmp_path / "asym.mp4")
    # content on top 2/3, black only on the bottom third → asymmetric.
    src = f"testsrc=size={W}x{H - 90}"
    subprocess.run(
        [_FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi", "-i",
         f"{src}:duration=6:rate=15",
         "-vf", f"pad={W}:{H}:0:0:black",  # content top, black band at bottom only
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-t", "6", clip],
        check=True, timeout=120,
    )
    assert pipeline._detect_letterbox_crop(clip) is None
    assert pipeline._strip_letterbox(clip) is False
