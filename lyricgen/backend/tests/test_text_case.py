"""Regression tests for the lyric text-case option."""

import pipeline


def test_lower_case_removes_sentence_initial_capital():
    assert pipeline._apply_text_case("na ra na, Na ra na", "lower") == "na ra na, na ra na"


def test_lower_preserves_interior_proper_nouns():
    assert pipeline._apply_text_case("Quizás llegue a Guinea", "lower") == "quizás llegue a Guinea"
    assert pipeline._apply_text_case("brilla el Sol", "lower") == "brilla el Sol"


def test_lower_preserves_dictionary_proper_nouns_after_punctuation():
    assert pipeline._apply_text_case("na ra na, Argentina", "lower") == "na ra na, Argentina"
    assert pipeline._apply_text_case("na ra na, México", "lower") == "na ra na, México"


def test_lower_normalizes_uniform_title_case_but_keeps_proper_noun():
    assert pipeline._apply_text_case("Te Amo Argentina", "lower") == "te amo Argentina"


def test_lower_applies_boundary_rule_without_whitespace():
    assert pipeline._apply_text_case("na ra na,Na ra na", "lower") == "na ra na,na ra na"


def test_lower_applies_boundary_rule_after_newline():
    assert pipeline._apply_text_case("na ra na\nNa ra na", "lower") == "na ra na\nna ra na"


def test_original_case_is_preserved():
    source = "na ra na, Na ra na"
    assert pipeline._apply_text_case(source, "original") == source


def test_default_case_remains_upper_for_legacy_jobs():
    assert pipeline._apply_text_case("na ra na", "upper") == "NA RA NA"


def test_short_renderer_accepts_text_case(monkeypatch):
    """The short must use the same case contract as the main render."""
    seen = []

    class FakeClip:
        size = (100, 20)

        def set_opacity(self, value):
            return self

        def set_position(self, value):
            return self

        def set_start(self, value):
            return self

        def set_end(self, value):
            return self

    def fake_text_clip(text, **kwargs):
        seen.append(text)
        return FakeClip()

    monkeypatch.setattr(pipeline, "TextClip", fake_text_clip)
    pipeline._make_short_text_clip("Na ra na", 0, 1, text_case="lower")

    assert seen == ["na ra na", "na ra na"]
