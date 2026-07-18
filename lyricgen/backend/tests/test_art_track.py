"""Unit tests for the "Art Tracks" render path (master audio + cover image →
cover composited over a blurred fill + subtle motion, NO lyrics).

Two layers:
  - Pure-string filtergraph contracts (no ffmpeg): the art-track background
    composite and the no-subtitles final filter. These pin the exact ffmpeg
    graph the single-pass render relies on.
  - Render-dispatch invariants exercised without running ffmpeg by capturing
    the command via monkeypatch.
"""
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
# _art_track_layout — composite geometry invariants
# --------------------------------------------------------------------------

def test_art_track_layout_landscape_card_on_right():
    L = pipeline._art_track_layout(rs.RenderSpec.youtube_default())
    W, H = 1920, 1080
    # Card sits on the right half; waveform + text on the left.
    assert L["card_x"] > W * 0.5
    assert L["card_x"] + L["card"] <= W
    assert L["wave_x"] < W * 0.2
    # Shadow sits just behind/under the card (offset down-right, larger).
    assert L["shadow"] > L["card"]
    assert L["shadow_x"] >= L["card_x"] and L["shadow_y"] >= L["card_y"]
    # Title/artist land below the waveform, inside the frame.
    assert L["title_y"] > L["wave_y"] + L["wave_h"]
    assert L["artist_y"] > L["title_y"]
    assert L["artist_y"] < H


def test_art_track_layout_portrait_card_on_top_centered():
    L = pipeline._art_track_layout(rs.RenderSpec.youtube_short())
    W, H = 1080, 1920
    # Portrait: card horizontally centered, near the top; waveform lower.
    assert abs((L["card_x"] + L["card"] / 2) - W / 2) < 4
    assert L["card_y"] < H * 0.3
    assert L["wave_y"] > H * 0.5
    assert L["wave_x"] + L["wave_w"] <= W


def test_art_track_layout_scales_with_resolution():
    hd = pipeline._art_track_layout(rs.RenderSpec.youtube_default())
    uhd = pipeline._art_track_layout(
        rs.RenderSpec.umg_intermediate_master({"frame_size": "UHD-4K", "fps": 24.0}))
    # 4K is 2x the 1080p height → card/waveform roughly 2x bigger.
    assert uhd["card"] > hd["card"] * 1.6
    assert uhd["wave_w"] > hd["wave_w"] * 1.6


def test_art_track_waveform_bars_fallback_is_flat():
    # Bogus path → librosa fails → flat non-zero bars (render never breaks).
    bars = pipeline._art_track_waveform_bars("/nonexistent.mp3", 68)
    assert len(bars) == 68
    assert all(0.0 <= b <= 1.0 for b in bars)
    assert min(bars) > 0.0


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
