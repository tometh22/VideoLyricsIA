from pathlib import Path

import pytest

from batch_manifest import build_manifest, parse_audio_filename
from batch_profiles import RenderProfileError, normalize_render_profile, pipeline_fields


AUDIO_DIR = Path("/Users/tomi/Downloads/Audio_Wavs 2")


def test_parser_handles_glued_code_and_versions():
    glued = parse_audio_filename("Que Pasó_Bersuit VergarabatARF149800014.wav")
    assert glued["title"] == "Que Pasó"
    assert glued["artist"] == "Bersuit Vergarabat"
    assert glued["technical_code"] == "ARF149800014"
    live = parse_audio_filename("Eso Es Real (Live)_Los Pericos_ARF040000028.wav")
    assert live["title"] == "Eso Es Real (Live)"
    assert live["version"] == "live"
    assert live["lookup_title"] == "Eso Es Real"


@pytest.mark.skipif(not AUDIO_DIR.exists(), reason="Universal WAV fixture folder is not mounted")
def test_real_universal_folder_has_no_unmapped_or_duplicate_codes():
    entries = build_manifest(AUDIO_DIR)
    assert len(entries) == 31  # current folder is guarded against accidental 30/31 drift
    assert all(entry.title and entry.artist and entry.technical_code for entry in entries)
    assert len({entry.technical_code for entry in entries}) == len(entries)
    assert len({entry.sha256 for entry in entries}) == len(entries)
    assert any(entry.fuzzy_lookup and entry.title.startswith("Instant-Taneas") for entry in entries)


def test_render_profile_is_strict_and_maps_fade():
    profile = normalize_render_profile({
        "font": "poppins-bold",
        "font_scale": 1.3,
        "text_case": "lower",
        "transition": "fade",
        "background_type": "photo",
        "movement": "foto-estatica",
        "effect": "bokeh",
        "style": "neon",
        "background_id": 42,
    })
    assert profile["movement_style"] == "foto-estatica"
    assert pipeline_fields(profile)["line_transition"] == "dissolve_blur"
    with pytest.raises(RenderProfileError):
        normalize_render_profile({"font": "comic-sans"})
    with pytest.raises(RenderProfileError):
        normalize_render_profile({"font": "poppins-bold", "movement": "foto-parallax"})
