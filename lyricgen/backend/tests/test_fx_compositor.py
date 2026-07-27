"""Unit tests for the effect-overlay + grade compositing filter builder.

Pure-string tests (no ffmpeg/moviepy) — they pin the filtergraph contract that
pipeline's single-pass libass render relies on, including the RGB-blend fix
(the magenta bug) and the input-index contract (bg=0, audio=1, fx=2).
"""
import fx_compositor as fx
import shutil
import subprocess

import numpy as np
import pytest


def test_effect_path_none_for_empty_or_unknown():
    assert fx.effect_path("") is None
    assert fx.effect_path("does-not-exist") is None


def test_effect_path_resolves_baked_loop():
    # snow.mp4 is baked by scripts/gen_fx_loops.py into assets/fx/.
    p = fx.effect_path("snow")
    assert p is not None and p.endswith("assets/fx/snow.mp4")
    # case + whitespace tolerant
    assert fx.effect_path("  SNOW ") == p


def test_expanded_effect_catalog_has_real_assets():
    added = {
        "aurora", "dust", "embers", "petals", "prism", "confetti",
        "film", "scanlines", "fog", "shapes",
        "liquid_glass", "caustics", "rgb_glitch", "neon_edge",
        "shadow_play", "kaleido", "halftone", "ink_reveal", "heatwave",
        "chromatic_pulse", "cutout_echo", "projector", "bass_pulse",
        "beat_flash", "chromatic_hit", "beat_ripple", "echo_hit",
    }
    assert added.issubset(set(fx.EFFECTS))
    for effect in added:
        path = fx.effect_path(effect)
        assert path is not None and path.endswith(f"assets/fx/{effect}.mp4")


def test_grade_filter_maps_palette_and_defaults_empty():
    assert fx.grade_filter("oscuro").startswith("eq=")
    assert "saturation=1.35" in fx.grade_filter("neon")
    assert fx.grade_filter("") == ""
    assert fx.grade_filter("auto") == ""
    assert fx.grade_filter("unknown-palette") == ""


def test_no_effect_keeps_simple_vf_path():
    vf, use_complex, extra = fx.build_video_filter(
        ass_basename="lyrics.ass", font_dir="/tmp/fonts",
        width=1920, height=1080, effect="", style="",
    )
    assert use_complex is False
    assert extra == []
    assert vf.startswith("subtitles=lyrics.ass:fontsdir=")
    assert "blend" not in vf


def test_no_effect_with_grade_prepends_eq():
    vf, use_complex, extra = fx.build_video_filter(
        ass_basename="lyrics.ass", font_dir="/tmp/fonts",
        width=1920, height=1080, effect="", style="calido",
    )
    assert use_complex is False
    assert vf.startswith("eq=") and ",subtitles=lyrics.ass" in vf


def test_effect_builds_filter_complex_with_rgb_blend_fix():
    fc, use_complex, extra = fx.build_video_filter(
        ass_basename="lyrics.ass", font_dir="/tmp/fonts",
        width=1280, height=720, effect="snow", style="",
    )
    assert use_complex is True
    # RGB-blend fix: both inputs to gbrp, screen blend, back to yuv420p.
    assert "[0:v]format=gbrp[bg]" in fc
    assert "scale=1280:720" in fc and "format=gbrp[fx]" in fc
    assert "blend=all_mode=screen:shortest=1[bl]" in fc
    assert "format=yuv420p[gr]" in fc
    # subtitles burn last, named output.
    assert fc.endswith("subtitles=lyrics.ass:fontsdir=/tmp/fonts[out]")
    # fx is input index 2, looped.
    assert extra[:3] == ["-stream_loop", "-1", "-i"]
    assert extra[3].endswith("assets/fx/snow.mp4")


def test_effect_with_grade_inserts_eq_before_yuv():
    fc, _, _ = fx.build_video_filter(
        ass_basename="lyrics.ass", font_dir="/tmp/fonts",
        width=1920, height=1080, effect="rain", style="oscuro",
    )
    assert "[bl]eq=" in fc and "format=yuv420p[gr]" in fc


def test_dark_effect_uses_multiply_and_editorial_opacity():
    fc, use_complex, _ = fx.build_video_filter(
        ass_basename="lyrics.ass", font_dir="/tmp/fonts",
        width=1920, height=1080, effect="shadow_play", style="",
    )
    assert use_complex is True
    assert "blend=all_mode=multiply:all_opacity=0.58" in fc
    assert fx.effect_blend("ink_reveal") == "multiply"
    assert fx.effect_blend("snow") == "screen"


def test_reactive_effect_tempo_matches_authored_loop():
    slow, _, _ = fx.build_video_filter(
        ass_basename="lyrics.ass", font_dir="/tmp/fonts",
        width=1920, height=1080, effect="beat_ripple", beat_bpm=90,
    )
    fast, _, _ = fx.build_video_filter(
        ass_basename="lyrics.ass", font_dir="/tmp/fonts",
        width=1920, height=1080, effect="beat_ripple", beat_bpm=150,
    )
    assert "setpts=(PTS-STARTPTS)*1.333333" in slow
    assert "setpts=(PTS-STARTPTS)*0.800000" in fast
    assert fx.effect_setpts("caustics", 90) == "setpts=PTS-STARTPTS"
    assert set(fx.REACTIVE_EFFECTS) == {
        "bass_pulse", "beat_flash", "chromatic_hit", "beat_ripple", "echo_hit",
    }


def test_fontsdir_path_is_escaped():
    fc, use_complex, _ = fx.build_video_filter(
        ass_basename="lyrics.ass", font_dir="/tmp/my fonts:weird",
        width=1920, height=1080, effect="stars",
    )
    # the ':' in the path must be backslash-escaped inside the filtergraph
    assert "fontsdir=/tmp/my fonts\\:weird[out]" in fc


# --- per-effect pre-blend gain (matrix test 2026-06-02) ----------------------
# Sparse/dim effects were imperceptible through the screen-blend on busy
# photos; they get an `eq`/`curves` boost BEFORE format=gbrp. Confetti is
# already bright enough and intentionally keeps the raw values.

def test_fx_gain_known_values():
    assert fx.fx_gain("stars").startswith("eq=contrast=2.0")
    assert fx.fx_gain("snow").startswith("eq=")
    assert fx.fx_gain("bokeh").startswith("curves=")  # mid-tone circles → curve
    assert fx.fx_gain("rain").startswith("curves=")
    assert fx.fx_gain("light").startswith("curves=")
    assert fx.fx_gain("dust").startswith("eq=")
    assert fx.fx_gain("embers").startswith("curves=")
    assert fx.fx_gain("petals").startswith("eq=")
    assert fx.fx_gain("prism").startswith("curves=")
    assert fx.fx_gain("confetti") == ""
    assert fx.fx_gain("film").startswith("curves=")
    assert fx.fx_gain("scanlines").startswith("curves=")
    assert fx.fx_gain("fog").startswith("curves=")
    assert fx.fx_gain("shapes").startswith("curves=")
    assert fx.fx_gain("shadow_play").startswith("gblur=")
    assert fx.fx_gain("") == ""
    assert fx.fx_gain("  STARS ").startswith("eq=")  # case/space tolerant


def test_dim_effect_inserts_gain_before_gbrp():
    fc, _, _ = fx.build_video_filter(
        ass_basename="l.ass", font_dir="/tmp/f",
        width=1080, height=1920, effect="stars", style="",
    )
    gain = fx.fx_gain("stars")
    assert f"setpts=PTS-STARTPTS,{gain},format=gbrp[fx]" in fc


def test_bright_effect_has_no_gain_step():
    fc, _, _ = fx.build_video_filter(
        ass_basename="l.ass", font_dir="/tmp/f",
        width=1080, height=1920, effect="confetti", style="",
    )
    assert "setpts=PTS-STARTPTS,format=gbrp[fx]" in fc  # no eq between them


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_light_overlay_travels_across_frame():
    """Regression: Luz used to keep its glows around fixed x/y anchors."""
    path = fx.effect_path("light")

    def centroid(at):
        result = subprocess.run(
            [
                "ffmpeg", "-loglevel", "error", "-ss", str(at), "-i", path,
                "-vf", "scale=96:54", "-frames:v", "1",
                "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1",
            ],
            capture_output=True,
            check=True,
            timeout=30,
        )
        frame = np.frombuffer(result.stdout, dtype=np.uint8).reshape(54, 96)
        weights = frame.astype(np.float64)
        yy, xx = np.indices(weights.shape)
        total = weights.sum()
        return np.array([(xx * weights).sum() / total, (yy * weights).sum() / total])

    positions = [centroid(t) for t in (0.5, 2.5, 4.5, 6.5)]
    max_travel = max(
        np.linalg.norm(a - b)
        for i, a in enumerate(positions)
        for b in positions[i + 1:]
    )
    assert max_travel > 25.0
