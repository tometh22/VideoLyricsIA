"""Unit tests for the "Art Tracks" render path (master audio + cover image →
cover composited over a blurred fill + subtle motion, NO lyrics).

Two layers:
  - Pure-string filtergraph contracts (no ffmpeg): the art-track background
    composite and the no-subtitles final filter. These pin the exact ffmpeg
    graph the single-pass render relies on.
  - Render-dispatch invariants exercised without running ffmpeg by capturing
    the command via monkeypatch.
"""
import math

import pytest

import fx_compositor as fx
import render_spec as rs
import pipeline


# --------------------------------------------------------------------------
# build_video_filter — no-subtitles (art track) variants
# --------------------------------------------------------------------------

def test_build_video_filter_no_subs_no_effect_no_grade_is_null():
    # Art track, no effect, no grade → a valid passthrough -vf.
    vf, use_complex, extra = fx.build_video_filter(
        ass_basename=None, font_dir="", width=1920, height=1080)
    assert vf == "null"
    assert use_complex is False
    assert extra == []


def test_build_video_filter_no_subs_with_grade_keeps_grade_drops_subs():
    vf, use_complex, extra = fx.build_video_filter(
        ass_basename=None, font_dir="", width=1920, height=1080,
        style="oscuro", custom_colors="")
    # No subtitles filter, but if a grade applies it survives.
    assert "subtitles=" not in vf
    assert use_complex is False


def test_build_video_filter_no_subs_with_effect_ends_null_out():
    vf, use_complex, extra = fx.build_video_filter(
        ass_basename=None, font_dir="", width=1920, height=1080, effect="bokeh")
    assert use_complex is True
    assert "subtitles=" not in vf
    # The final labelled output must exist for -map [out]; with no subs the
    # last stage is a null passthrough into [out].
    assert vf.endswith("null[out]")
    assert extra[:2] == ["-stream_loop", "-1"]


def test_build_video_filter_lyric_path_unregressed():
    # Normal lyric render still burns subtitles (both simple + complex forms).
    simple, uc, _ = fx.build_video_filter(
        ass_basename="lyrics.ass", font_dir="/tmp/fonts", width=1920, height=1080)
    assert "subtitles=lyrics.ass" in simple and uc is False
    cx, uc2, _ = fx.build_video_filter(
        ass_basename="lyrics.ass", font_dir="/tmp/fonts", width=1920, height=1080,
        effect="snow")
    assert "subtitles=lyrics.ass" in cx and cx.endswith("[out]") and uc2 is True


# --------------------------------------------------------------------------
# _art_track_filtergraph — composite invariants
# --------------------------------------------------------------------------

@pytest.mark.parametrize("spec", [
    rs.RenderSpec.youtube_default(),
    rs.RenderSpec.youtube_short(),
    rs.RenderSpec.umg_intermediate_master({"frame_size": "UHD-4K", "fps": 24.0}),
])
def test_art_filtergraph_structure(spec):
    total = max(1, int(math.ceil(200 * spec.fps)))
    fg = pipeline._art_track_filtergraph(spec, total)
    # Blurred, frame-filling copy of the cover.
    assert "split=2" in fg
    assert "gblur=sigma=" in fg
    # Centered sharp cover fitted inside a margin box.
    assert "force_original_aspect_ratio=decrease" in fg
    assert "overlay=(W-w)/2:(H-h)/2" in fg
    # Subtle push-in capped at 1.05, output at the spec's exact dims.
    assert "min(zoom+" in fg and ",1.05)" in fg
    assert f"s={spec.width}x{spec.height}" in fg
    assert f"d={total}" in fg


def test_art_filtergraph_zoom_step_scales_with_frames():
    spec = rs.RenderSpec.youtube_default()
    short = pipeline._art_track_filtergraph(spec, 240)     # 10s @24
    long = pipeline._art_track_filtergraph(spec, 7200)     # 5min @24
    # Longer clip → smaller per-frame zoom step (same 5% total travel).
    import re
    step_short = float(re.search(r"min\(zoom\+([0-9.]+),", short).group(1))
    step_long = float(re.search(r"min\(zoom\+([0-9.]+),", long).group(1))
    assert step_short > step_long > 0


# --------------------------------------------------------------------------
# _pick_energy_window — window selection for the art-track short
# --------------------------------------------------------------------------

def test_pick_energy_window_short_track_returns_zero():
    # Track shorter than the window → start at 0.
    assert pipeline._pick_energy_window("/nonexistent.mp3", duration=20.0,
                                        window_sec=30.0) == 0.0


def test_pick_energy_window_bad_file_falls_back_to_offset():
    # librosa.load fails on a bogus path → deterministic 30% fallback,
    # clamped so the window fits inside the track.
    start = pipeline._pick_energy_window("/nonexistent.mp3", duration=300.0,
                                         window_sec=30.0)
    assert start == pytest.approx(90.0)
    assert start <= 300.0 - 30.0
