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
    # Hard safe zone: the text column must end well before the card starts
    # (regression guard for the title-under-the-card bug).
    assert L["text_x"] + L["text_max_w"] <= L["card_x"] - 50
    assert L["text_align"] == "left"
    # Bar geometry is layout-owned so PIL and ffmpeg always agree.
    assert L["n_bars"] * L["pitch"] <= L["wave_w"]
    assert 3 <= L["bar_w"] < L["pitch"]


def test_art_track_layout_portrait_single_centered_axis():
    L = pipeline._art_track_layout(rs.RenderSpec.youtube_short())
    W, H = 1080, 1920
    # ONE centered axis: card, waveform and text all share the center line.
    assert abs((L["card_x"] + L["card"] / 2) - W / 2) < 4
    assert abs((L["wave_x"] + L["wave_w"] / 2) - W / 2) < 4
    assert L["text_align"] == "center"
    assert abs(L["text_x"] - W / 2) < 4
    # Card in the upper-middle, waveform in the lower-middle band.
    assert L["card_y"] < H * 0.3
    assert 0.55 * H <= L["wave_y"] <= 0.70 * H
    # Platform safe areas: nothing under 0.85H (captions/UI) nor past 0.88W.
    assert L["artist_y"] + L["artist_size"] < 0.85 * H
    assert L["legal_y"] < 0.85 * H
    assert L["wave_x"] + L["wave_w"] <= 0.88 * W
    assert L["card_x"] + L["card"] <= 0.88 * W


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


# --------------------------------------------------------------------------
# _render_art_track — dispatch invariants (ffmpeg captured, never run)
# --------------------------------------------------------------------------

def _render_with_stubs(monkeypatch, tmp_path, spec, **render_kwargs):
    """Run _render_art_track with every side-effect stubbed; returns the
    captured state (ffmpeg cmd, DSP args, frames dir)."""
    import os
    import numpy as np
    import art_track_wave

    calls = {}

    def fake_compute(mp3, *, n_bars, n_frames, fps, win_start=0.0, win_dur=None):
        calls["compute"] = dict(n_bars=n_bars, n_frames=n_frames, fps=fps,
                                win_start=win_start, win_dur=win_dur)
        return np.zeros((n_frames, n_bars), dtype=np.float32)

    def fake_write(frames, out_dir, *, w, h, pitch, bar_w):
        os.makedirs(out_dir, exist_ok=True)
        open(os.path.join(out_dir, "w000000.png"), "wb").write(b"x")
        calls["frames_dir"] = out_dir
        return os.path.join(out_dir, "w%06d.png")

    def fake_run(cmd, label=None, timeout=None, output_path=None, cwd=None):
        calls["cmd"] = cmd
        open(output_path, "wb").write(b"0" * 1024)

    def fake_base(cover, out_path, **k):
        open(out_path, "wb").write(b"png")
        return out_path

    monkeypatch.setattr(art_track_wave, "compute_bar_frames", fake_compute)
    monkeypatch.setattr(art_track_wave, "write_wave_frames", fake_write)
    monkeypatch.setattr(pipeline, "run_checked", fake_run)
    monkeypatch.setattr(pipeline, "_validate_rendered_mp4", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_build_art_track_base", fake_base)

    pipeline._render_art_track(
        "cover.jpg", "song.mp3", str(tmp_path), spec=spec,
        artist="A", song_title="T", **render_kwargs)
    return calls


def test_render_art_track_dispatch_uses_reactive_strip(monkeypatch, tmp_path):
    import math
    import os
    spec = rs.RenderSpec.youtube_default()
    calls = _render_with_stubs(
        monkeypatch, tmp_path, spec,
        duration=12.0, win_start=30.0, win_dur=12.0)
    cmd = calls["cmd"]
    joined = " ".join(cmd)
    # The scrolling showwaves oscilloscope is gone — the wave is a
    # precomputed strip sequence overlaid with eof_action=repeat.
    assert "showwaves" not in joined and "geq" not in joined
    assert "w%06d.png" in joined
    assert "eof_action=repeat" in joined
    assert "-shortest" in cmd
    # Audio is now the third input.
    assert "2:a" in cmd
    # Both video inputs run at the spec fps (≤32fps → no half-rate).
    frates = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-framerate"]
    assert frates == [spec.fps_str, spec.fps_str]
    # The DSP analyzed the SAME window the audio input seeks to.
    assert calls["compute"]["win_start"] == 30.0
    assert calls["compute"]["win_dur"] == 12.0
    assert calls["compute"]["n_frames"] == math.ceil(12.0 * 24)
    # Transient strip frames are cleaned up even though ffmpeg "succeeded".
    assert not os.path.exists(calls["frames_dir"])


def test_render_art_track_half_rate_wave_at_high_fps(monkeypatch, tmp_path):
    import dataclasses
    import math
    spec60 = dataclasses.replace(rs.RenderSpec.youtube_default(), fps=60.0)
    calls = _render_with_stubs(monkeypatch, tmp_path, spec60, duration=10.0)
    frates = [calls["cmd"][i + 1]
              for i, a in enumerate(calls["cmd"]) if a == "-framerate"]
    # Base at 60, wave strip at half rate (overlay holds frames).
    assert frates == [spec60.fps_str, "30"]
    assert calls["compute"]["n_frames"] == math.ceil(10.0 * 30)
