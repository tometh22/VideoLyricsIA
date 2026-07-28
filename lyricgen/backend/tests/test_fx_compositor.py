"""Unit tests for the effect-overlay + grade compositing filter builder.

Pure-string tests (no ffmpeg/moviepy) — they pin the filtergraph contract that
pipeline's single-pass libass render relies on, including the RGB-blend fix
(the magenta bug) and the input-index contract (bg=0, audio=1, fx=2).
"""
import fx_compositor as fx
import shutil
import subprocess
import sys
import types

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
        "foto_viva", "beat_flash", "chromatic_hit", "beat_ripple", "echo_hit",
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
    assert (
        "[0:v]scale=1280:720:force_original_aspect_ratio=increase,"
        "crop=1280:720,format=gbrp[bg]"
    ) in fc
    assert "scale=1280:720" in fc and "format=gbrp[fxraw]" in fc
    assert "[fxraw]null[fx]" in fc
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
    assert "blend=all_mode=multiply:all_opacity=0.34" in fc
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


def test_reactive_effect_aligns_phase_and_uses_exact_energy_weighted_beats():
    rhythm = fx.EffectRhythm(120.0, (0.10, 0.60), (1.0, 0.55))
    fc, _, _ = fx.build_video_filter(
        ass_basename=None, font_dir="", width=640, height=360,
        effect="beat_ripple", rhythm=rhythm,
    )
    # 120 BPM source hit period is .5 s. Trimming .4 s makes its next hit land
    # at detector timestamp .1, instead of merely running at the same BPM.
    assert "trim=start=0.400000,setpts=(PTS-STARTPTS)*1.000000" in fc
    assert "abs(T-0.1000)" in fc and "abs(T-0.6000)" in fc
    assert "1.000*(1-abs" in fc and "0.550*(1-abs" in fc
    assert "[fxraw][beatmask]blend=all_mode=multiply" in fc
    assert fx.effect_strength_at(rhythm, 0.10) == pytest.approx(1.0)
    assert fx.effect_strength_at(rhythm, 0.35) == pytest.approx(0.1)


def test_rhythm_rebases_to_short_window_without_losing_energy():
    rhythm = fx.EffectRhythm(100.0, (4.8, 5.2, 5.8, 7.1), (.5, .7, 1.0, .6))
    short = fx.rhythm_for_window(rhythm, 5.0, 2.0)
    assert short.bpm == 100.0
    assert short.beats == pytest.approx((.2, .8))
    assert short.strengths == pytest.approx((.7, 1.0))


def test_invalid_or_empty_beat_grid_uses_safe_120_bpm_fallback(monkeypatch):
    fake_beat_snap = types.SimpleNamespace(
        detect_beats=lambda _path: (0.0, [])
    )
    monkeypatch.setitem(sys.modules, "beat_snap", fake_beat_snap)
    fx._detect_rhythm_cached.cache_clear()
    rhythm = fx._detect_rhythm_cached("/tmp/silent.mp3", 1)
    assert rhythm == fx.EffectRhythm(120.0, (), ())
    fx._detect_rhythm_cached.cache_clear()


@pytest.mark.parametrize(
    ("effect", "contract"),
    [
        ("liquid_glass", "displace=edge=mirror"),
        ("heatwave", "displace=edge=mirror"),
        ("rgb_glitch", "colorchannelmixer"),
        ("neon_edge", "edgedetect=mode=wires"),
        ("kaleido", "vstack=inputs=2"),
        ("halftone", "flags=neighbor"),
        ("ink_reveal", "maskedmerge"),
        ("chromatic_pulse", "colorchannelmixer"),
        ("cutout_echo", "scale=1203:676"),
        ("projector", "vignette=PI/5.2"),
        ("foto_viva", "maskedmerge"),
    ],
)
def test_photo_transform_effects_derive_pixels_from_background(effect, contract):
    fc, use_complex, _ = fx.build_video_filter(
        ass_basename=None, font_dir="", width=1280, height=720, effect=effect,
    )
    assert use_complex is True
    assert effect in fx.PIXEL_TRANSFORM_EFFECTS
    assert contract in fc
    # An auxiliary raw loop may remain, but the visible graph must explicitly
    # split/filter the chosen photo instead of only doing [bg][fx] blend.
    assert "[bg]" in fc and "[bl]" in fc


@pytest.mark.parametrize("effect", list(fx.PIXEL_TRANSFORM_EFFECTS))
def test_moviepy_fallback_also_transforms_selected_photo(effect):
    yy, xx = np.indices((90, 160))
    photo = np.stack(
        [(xx * 2) % 255, (yy * 3) % 255, ((xx + yy) * 2) % 255], axis=2
    ).astype(np.uint8)
    layer = np.stack(
        [(xx + 40) % 255, (yy + 70) % 255, (xx * 0 + 120)], axis=2
    ).astype(np.uint8)
    output = fx.transform_photo_frame(photo, effect, 0.65, layer)
    assert output.shape == photo.shape
    assert np.abs(output.astype(np.int16) - photo.astype(np.int16)).mean() > 1.0


def test_foto_viva_is_generative_first_but_keeps_a_local_transform_fallback():
    assert fx.is_generative_effect(" FOTO_VIVA ")
    assert fx.is_pixel_transform("foto_viva")
    assert not fx.is_generative_effect("rain")


def test_corrected_editorial_effects_keep_the_photo_readable():
    kaleido, _, _ = fx.build_video_filter(
        ass_basename=None, font_dir="", width=1280, height=720, effect="kaleido",
    )
    ink, _, _ = fx.build_video_filter(
        ass_basename=None, font_dir="", width=1280, height=720, effect="ink_reveal",
    )
    chromatic, _, _ = fx.build_video_filter(
        ass_basename=None, font_dir="", width=1280, height=720,
        effect="chromatic_pulse",
    )
    # Kaleido's auxiliary rays are now only a barely-visible accent.
    assert "all_opacity=0.03" in kaleido
    # Ink leaves the original photograph as the neutral state and applies the
    # stylized treatment only inside the authored brush mask.
    assert "[inkbase][inkwash][inkmask]maskedmerge[inkmerged]" in ink
    assert "[inkmerged][inktexture]blend=all_mode=multiply:all_opacity=0.12" in ink
    # Chromatic Pulse isolates shifted-photo differences (contours) instead of
    # screen-blending whole red/blue copies and washing the frame magenta.
    assert chromatic.count("blend=all_mode=difference") == 2
    assert "all_opacity=0.22" in chromatic


def test_neon_and_halftone_do_not_apply_full_frame_color_washes():
    neon, _, _ = fx.build_video_filter(
        ass_basename=None, font_dir="", width=1280, height=720,
        effect="neon_edge",
    )
    halftone, _, _ = fx.build_video_filter(
        ass_basename=None, font_dir="", width=1280, height=720,
        effect="halftone",
    )
    assert "mode=wires" in neon
    assert "mode=colormix" not in neon
    # `eq=saturation` on planar GBR treated the green plane as luminance and
    # turned the delivered halftone sample solid green.
    assert "saturation=" not in halftone
    assert "all_opacity=0.36" in halftone


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
    assert f"setpts=PTS-STARTPTS,{gain},format=gbrp[fxraw]" in fc


def test_bright_effect_has_no_gain_step():
    fc, _, _ = fx.build_video_filter(
        ass_basename="l.ass", font_dir="/tmp/f",
        width=1080, height=1920, effect="confetti", style="",
    )
    assert "setpts=PTS-STARTPTS,format=gbrp[fxraw]" in fc  # no eq between them


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
