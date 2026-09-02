"""Tests for _darken_prompt_for_effect — biases the Imagen scene prompt toward
a dark/low-key canvas when a luminous particle effect will be composited, so
stars/bokeh/snow/light read (matrix test 2026-06-02).
"""
import pipeline

BASE = "A campfire on a beach at golden hour, warm tones, wide shot."


def test_no_effect_is_noop():
    assert pipeline._darken_prompt_for_effect(BASE, "") == BASE
    assert pipeline._darken_prompt_for_effect(BASE, "none") == BASE
    assert pipeline._darken_prompt_for_effect(BASE, "unknown-fx") == BASE


def test_effect_appends_lowkey_clause():
    for eff in ("stars", "bokeh", "snow", "light", "rain", "aurora"):
        out = pipeline._darken_prompt_for_effect(BASE, eff)
        assert out.startswith(BASE.rstrip()), eff
        assert len(out) > len(BASE), eff
        assert "LOW-KEY" in out and "dark negative space" in out, eff


def test_case_and_space_tolerant():
    assert pipeline._darken_prompt_for_effect(BASE, "  STARS ") != BASE


def test_multiply_effect_keeps_bright_scene_available():
    """Dark ink/shadow effects need highlights to multiply against."""
    for eff in ("shadow_play", "halftone", "ink_reveal"):
        assert pipeline._darken_prompt_for_effect(BASE, eff) == BASE


def test_does_not_use_scene_guard_forbidden_words():
    """The scene guard forbids night/urban/alley/street/pavement cliches —
    the darkening must be a grading/mood instruction, not a scene swap."""
    out = pipeline._darken_prompt_for_effect(BASE, "stars").lower()
    appended = out[len(BASE):]
    for forbidden in ("night", "urban", "alley", "street", "pavement", "graffiti"):
        assert forbidden not in appended, forbidden


def test_original_subject_preserved():
    out = pipeline._darken_prompt_for_effect(BASE, "bokeh")
    assert "campfire on a beach" in out  # subject kept, only graded darker
