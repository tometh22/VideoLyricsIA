"""Regression test for the unplayable-video bug: the render output MUST be
written with `-movflags +faststart` so the moov atom lands at the FRONT of
the MP4 and the browser can start progressive playback. Without it a large
main video (~124 MB) never starts (infinite spinner); confirmed by ffprobe
2026-05-21 (moov at byte 124.7M).

Source-level guard (no ffmpeg/libass needed in CI): the libass ffmpeg pass
and the moviepy write_videofile calls must request faststart.
"""

import os
import re

_PIPELINE = os.path.join(os.path.dirname(__file__), "..", "pipeline.py")


def _src():
    with open(_PIPELINE, encoding="utf-8") as f:
        return f.read()


def test_ass_pass_has_faststart():
    src = _src()
    fn = src[src.index("def _render_lyrics_ass"):src.index("def generate_lyric_video")]
    assert '"+faststart"' in fn or "+faststart" in fn, (
        "_render_lyrics_ass must pass -movflags +faststart"
    )
    assert '"-movflags"' in fn


def test_deliverable_moviepy_writes_have_faststart():
    """The user-facing deliverable writes — the main YouTube video
    (out_path lyric_video.mp4) and the short (short.mp4) — must request
    +faststart. Intermediate/fallback renders (gradient bg, etc.) don't
    need it. We assert the two deliverable write_videofile blocks (the ones
    with preset='veryfast' + out_path) carry +faststart."""
    src = _src()
    deliverable = [
        m.start() for m in re.finditer(r"\.write_videofile\(\s*\n\s*out_path,", src)
    ]
    assert deliverable, "expected deliverable write_videofile(out_path, ...) calls"
    for pos in deliverable:
        block = src[pos:pos + 400]
        if "prores" in block.lower():
            continue  # ProRes master carries its own ffmpeg_params
        assert "+faststart" in block, (
            "deliverable write_videofile near offset %d lacks +faststart" % pos
        )


def test_faststart_appears_for_all_three_deliverable_paths():
    """libass pass + main moviepy + short moviepy → at least 3 faststart uses."""
    assert _src().count("+faststart") >= 3


def test_ass_render_validates_output_before_returning():
    """The libass pass must ffprobe-validate its output (streams, duration,
    faststart) and let the caller fall back to moviepy on a malformed
    exit-0 render. Guard: _render_lyrics_ass calls _validate_rendered_mp4
    before returning, and the validator checks the moov/faststart."""
    src = _src()
    fn = src[src.index("def _render_lyrics_ass"):src.index("def _resolve_title_song")]
    assert "_validate_rendered_mp4(out_path" in fn, (
        "_render_lyrics_ass must validate its output before returning"
    )
    validator = src[src.index("def _validate_rendered_mp4"):src.index("def _render_lyrics_ass")]
    assert "moov" in validator and "faststart" in validator
    assert "no video stream" in validator and "no audio stream" in validator
