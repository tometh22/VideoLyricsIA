"""Tests for render_spec.py — UMG profile validation and RenderSpec construction.

Highest-stakes code path (UMG asset spec compliance) had zero coverage. These
tests lock in: every valid UMG combo constructs a correct RenderSpec, every
invalid combo is rejected pre-render.
"""

import pytest

from render_spec import (
    FPS_RATIONAL,
    UMG_FPS,
    UMG_FRAME_SIZES,
    UMG_PRORES_PROFILES,
    RenderSpec,
    umg_catalog,
    validate_umg_config,
)


# ---------------------------------------------------------------------------
# RenderSpec.youtube_default / youtube_short
# ---------------------------------------------------------------------------


def test_youtube_default_is_h264_1080p_24fps_yuv420p():
    spec = RenderSpec.youtube_default()
    assert spec.profile == "youtube"
    assert (spec.width, spec.height) == (1920, 1080)
    assert spec.fps == 24.0
    assert spec.codec == "libx264"
    assert spec.pix_fmt == "yuv420p"
    assert spec.audio_codec == "aac"
    assert spec.container == "mp4"
    assert spec.dar == (16, 9)


def test_youtube_short_is_vertical_9x16():
    spec = RenderSpec.youtube_short()
    assert (spec.width, spec.height) == (1080, 1920)
    assert spec.dar == (9, 16)
    assert spec.codec == "libx264"
    assert spec.container == "mp4"


# ---------------------------------------------------------------------------
# RenderSpec.umg — happy paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("frame_size,expected_dims,expected_dar", [
    ("DCI-4K", (4096, 2160), (256, 135)),
    ("UHD-4K", (3840, 2160), (16, 9)),
    ("DCI-2K", (2048, 1080), (256, 135)),
    ("HD",     (1920, 1080), (16, 9)),
])
def test_umg_all_frame_sizes_construct(frame_size, expected_dims, expected_dar):
    spec = RenderSpec.umg(frame_size=frame_size, fps=24.0, prores_profile=3)
    assert (spec.width, spec.height) == expected_dims
    assert spec.dar == expected_dar
    assert spec.profile == "umg"
    assert spec.codec == "prores_ks"
    assert spec.container == "mov"
    assert spec.audio_codec == "pcm_s24le"


@pytest.mark.parametrize("fps", list(UMG_FPS))
def test_umg_all_fps_accepted(fps):
    spec = RenderSpec.umg(frame_size="HD", fps=fps, prores_profile=3)
    assert spec.fps == fps


@pytest.mark.parametrize("profile,expected_pix_fmt", [
    (3, "yuv422p10le"),  # ProRes 422 HQ
    (4, "yuv444p10le"),  # ProRes 4444
    (5, "yuv444p10le"),  # ProRes 4444 XQ
])
def test_umg_prores_profile_to_pix_fmt(profile, expected_pix_fmt):
    spec = RenderSpec.umg(frame_size="HD", fps=24.0, prores_profile=profile)
    assert spec.pix_fmt == expected_pix_fmt
    assert spec.prores_profile == profile


# ---------------------------------------------------------------------------
# RenderSpec.umg — rejection paths
# ---------------------------------------------------------------------------


def test_umg_rejects_invalid_frame_size():
    with pytest.raises(ValueError, match="Invalid UMG frame_size"):
        RenderSpec.umg(frame_size="720p", fps=24.0, prores_profile=3)


def test_umg_rejects_invalid_fps():
    # 48 fps is a real cinema rate but not in UMG's accepted list.
    with pytest.raises(ValueError, match="Invalid UMG fps"):
        RenderSpec.umg(frame_size="HD", fps=48.0, prores_profile=3)


def test_umg_rejects_invalid_prores_profile():
    # Profile 0 (Proxy), 1 (LT), 2 (Standard) are not UMG-accepted.
    with pytest.raises(ValueError, match="Invalid UMG prores_profile"):
        RenderSpec.umg(frame_size="HD", fps=24.0, prores_profile=2)


# ---------------------------------------------------------------------------
# fps_str rational fractions (R1 — must be set correctly for UMG compliance)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fps,expected", [
    (23.976, "24000/1001"),
    (29.97,  "30000/1001"),
    (59.94,  "60000/1001"),
])
def test_fps_str_returns_rational_for_fractional_rates(fps, expected):
    spec = RenderSpec.umg(frame_size="HD", fps=fps, prores_profile=3)
    assert spec.fps_str == expected


@pytest.mark.parametrize("fps", [24.0, 25.0, 30.0, 50.0, 60.0])
def test_fps_str_returns_decimal_for_integer_rates(fps):
    spec = RenderSpec.umg(frame_size="HD", fps=fps, prores_profile=3)
    assert spec.fps_str == str(fps)


# ---------------------------------------------------------------------------
# validate_umg_config — pre-flight rejection
# ---------------------------------------------------------------------------


def test_validate_umg_config_passes_valid_combo():
    assert validate_umg_config("HD", 24.0, 3) == []


def test_validate_umg_config_collects_all_errors():
    errors = validate_umg_config("720p", 48.0, 99)
    # Should report all three problems, not stop at first.
    assert len(errors) == 3
    assert any("frame_size" in e for e in errors)
    assert any("fps" in e for e in errors)
    assert any("prores_profile" in e for e in errors)


# ---------------------------------------------------------------------------
# umg_catalog — frontend dropdown source
# ---------------------------------------------------------------------------


def test_umg_catalog_exposes_all_options():
    cat = umg_catalog()
    assert len(cat["frame_sizes"]) == len(UMG_FRAME_SIZES)
    assert len(cat["fps"]) == len(UMG_FPS)
    assert len(cat["prores_profiles"]) == len(UMG_PRORES_PROFILES)

    keys = [fs["key"] for fs in cat["frame_sizes"]]
    assert {"DCI-4K", "UHD-4K", "DCI-2K", "HD"} == set(keys)

    profile_keys = [p["key"] for p in cat["prores_profiles"]]
    assert {3, 4, 5} == set(profile_keys)


def test_umg_catalog_dar_is_string_formatted():
    """Frontend expects 'X:Y' strings, not tuples."""
    cat = umg_catalog()
    for fs in cat["frame_sizes"]:
        assert ":" in fs["dar"]


# ---------------------------------------------------------------------------
# Text scale property — used by _make_text_clip for proportional fonts
# ---------------------------------------------------------------------------


def test_text_scale_baseline_at_1080p():
    spec = RenderSpec.umg(frame_size="HD", fps=24.0, prores_profile=3)
    assert spec.text_scale == 1.0


def test_text_scale_doubles_at_2160p():
    spec = RenderSpec.umg(frame_size="UHD-4K", fps=24.0, prores_profile=3)
    assert spec.text_scale == 2.0
