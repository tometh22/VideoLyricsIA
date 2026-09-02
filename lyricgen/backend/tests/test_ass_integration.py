"""Integration test for the libass fast lyric path (_render_lyrics_ass).

Runs the REAL ffmpeg + libass pipeline on a tiny synthetic background +
audio, and asserts a valid video comes out with the right dimensions and
an audio stream. This is the test that proves the ffmpeg `subtitles`
filter command actually executes (the riskiest unknown: filtergraph
escaping + libass finding the single-font dir) — it can only run where
ffmpeg is installed (CI Docker image), so it skips cleanly otherwise.

Visual parity (does the libass overlay LOOK like the moviepy one) is a
human check on staging behind LYRIC_RENDER_ENGINE=ass; this test only
guarantees the path produces a structurally valid file.
"""

import os
import shutil
import subprocess

import pytest

_FFMPEG = shutil.which("ffmpeg")
_FFPROBE = shutil.which("ffprobe")


def _ffmpeg_has_libass():
    if not _FFMPEG:
        return False
    try:
        output = subprocess.run(
            [_FFMPEG, "-hide_banner", "-filters"], capture_output=True,
            text=True, timeout=20,
        ).stdout
    except Exception:
        return False
    return any(
        line.split()[1:2] == ["subtitles"]
        for line in output.splitlines() if line.strip()
    )

pytestmark = pytest.mark.skipif(
    not (_FFMPEG and _FFPROBE and _ffmpeg_has_libass()),
    reason="ffmpeg/ffprobe with libass not available",
)


def _probe(path, entries):
    out = subprocess.check_output(
        [_FFPROBE, "-v", "error", "-show_entries", entries,
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        text=True,
    )
    return out.strip().splitlines()


def test_render_lyrics_ass_produces_valid_video(tmp_path):
    pipeline = pytest.importorskip("pipeline")  # heavy deps (moviepy etc.)
    from render_spec import RenderSpec

    # Synthesize a 2s background video + 2s audio with ffmpeg.
    bg = str(tmp_path / "bg.mp4")
    audio = str(tmp_path / "audio.m4a")
    subprocess.run(
        [_FFMPEG, "-y", "-f", "lavfi",
         "-i", "testsrc=size=320x180:rate=24:duration=2",
         "-pix_fmt", "yuv420p", bg],
        check=True, capture_output=True,
    )
    subprocess.run(
        [_FFMPEG, "-y", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=2", "-c:a", "aac", audio],
        check=True, capture_output=True,
    )

    font = os.path.join(os.path.dirname(pipeline.__file__), "fonts", "Oswald-Bold.ttf")
    assert os.path.isfile(font), "expected a bundled font for the test"

    segments = [
        {"start": 0.2, "end": 1.0, "text": "hola mundo"},
        {"start": 1.0, "end": 1.8, "text": "segunda línea"},
    ]
    spec = RenderSpec.youtube_default()

    out = pipeline._render_lyrics_ass(
        bg, audio, segments, str(tmp_path), 2.0,
        spec=spec, font_path=font, text_case="upper",
        lyric_transition="fade",
    )

    # Output exists and is non-trivial.
    assert os.path.isfile(out)
    assert os.path.getsize(out) > 1024

    # Right dimensions (libass + scale honored the spec).
    w, h = _probe(out, "stream=width,height")[:2]
    assert int(w) == spec.width and int(h) == spec.height

    # Has both a video and an audio stream.
    codecs = _probe(out, "stream=codec_type")
    assert "video" in codecs and "audio" in codecs

    # The ASS file was written next to the output and has our dialogues.
    ass_path = os.path.join(str(tmp_path), "lyrics.ass")
    assert os.path.isfile(ass_path)
    ass_text = open(ass_path, encoding="utf-8").read()
    assert "Dialogue:" in ass_text
    assert "HOLA MUNDO" in ass_text  # upper-cased


def test_ffmpeg_filter_escape_handles_colons():
    pipeline = pytest.importorskip("pipeline")
    assert pipeline._ffmpeg_filter_escape("/tmp/a:b") == "/tmp/a\\:b"
    assert pipeline._ffmpeg_filter_escape("/plain/path") == "/plain/path"
