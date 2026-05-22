"""Unit tests for the effect-overlay + grade compositing filter builder.

Pure-string tests (no ffmpeg/moviepy) — they pin the filtergraph contract that
pipeline's single-pass libass render relies on, including the RGB-blend fix
(the magenta bug) and the input-index contract (bg=0, audio=1, fx=2).
"""
import fx_compositor as fx


def test_effect_path_none_for_empty_or_unknown():
    assert fx.effect_path("") is None
    assert fx.effect_path("does-not-exist") is None


def test_effect_path_resolves_baked_loop():
    # snow.mp4 is baked by scripts/gen_fx_loops.py into assets/fx/.
    p = fx.effect_path("snow")
    assert p is not None and p.endswith("assets/fx/snow.mp4")
    # case + whitespace tolerant
    assert fx.effect_path("  SNOW ") == p


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


def test_fontsdir_path_is_escaped():
    fc, use_complex, _ = fx.build_video_filter(
        ass_basename="lyrics.ass", font_dir="/tmp/my fonts:weird",
        width=1920, height=1080, effect="stars",
    )
    # the ':' in the path must be backslash-escaped inside the filtergraph
    assert "fontsdir=/tmp/my fonts\\:weird[out]" in fc
