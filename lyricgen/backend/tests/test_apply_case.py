"""Tests for _apply_case / _smart_lower — text-case transforms applied to
each lyric line before rendering.

Pins the fix for agus.cafisi / Babasónicos 2026-05-20: under text_case=
'lower', the proper noun "Guinea" rendered as "guinea". A blind
text.lower() flattened proper nouns; _smart_lower keeps interior capitals
the operator typed while still lowercasing the line's first word for the
all-lowercase aesthetic.
"""

import pipeline


def test_upper_uppercases_everything():
    assert pipeline._apply_case("quizás llegue a Guinea", "upper") == "QUIZÁS LLEGUE A GUINEA"


def test_original_keeps_text_untouched():
    assert pipeline._apply_case("Quizás llegue a Guinea", "original") == "Quizás llegue a Guinea"


def test_lower_preserves_interior_proper_noun():
    """The core bug: 'Guinea' mid-line must survive the lowercase aesthetic."""
    assert pipeline._apply_case("quizás llegue a Guinea", "lower") == "quizás llegue a Guinea"


def test_lower_lowercases_first_word_even_if_capitalized():
    """Sentence-initial capitals are grammar, not intent — first word goes
    lowercase to keep the all-lowercase look."""
    assert pipeline._apply_case("Quizás llegue", "lower") == "quizás llegue"


def test_lower_proper_noun_first_word_is_lowercased():
    """Documented tradeoff: a proper noun as the FIRST word loses its capital
    so the line still starts lowercase. Operator can switch to 'original'
    if they need it preserved."""
    assert pipeline._apply_case("Guinea me espera", "lower") == "guinea me espera"


def test_lower_all_lowercase_input_is_noop():
    assert pipeline._apply_case("solo letras minúsculas", "lower") == "solo letras minúsculas"


def test_lower_preserves_multiple_interior_capitals():
    assert pipeline._apply_case("vamos a Buenos Aires hoy", "lower") == "vamos a Buenos Aires hoy"


def test_lower_preserves_internal_whitespace():
    """Double spaces the renderer may rely on for layout must survive."""
    assert pipeline._apply_case("a  b  Guinea", "lower") == "a  b  Guinea"


def test_lower_empty_string():
    assert pipeline._apply_case("", "lower") == ""


# --- uniformly title-cased / ALL-CAPS sources (matrix test 2026-06-02) -------
# When the SOURCE title-cases or upper-cases the whole line, the capitals are
# formatting, not proper nouns — honour 'lower' as a true all-lowercase look
# (except for known proper nouns, see below).

def test_lower_uniformly_titlecased_becomes_all_lower():
    assert pipeline._apply_case("Caminando Bajo La Lluvia", "lower") == "caminando bajo la lluvia"


def test_lower_allcaps_becomes_all_lower():
    assert pipeline._apply_case("CAMINANDO BAJO LA LLUVIA", "lower") == "caminando bajo la lluvia"


def test_lower_titlecased_other_line():
    assert pipeline._apply_case("Tu Voz Me Llama Desde Lejos", "lower") == "tu voz me llama desde lejos"


def test_lower_titlecased_preserves_country():
    """A country in an otherwise title-cased line keeps its capital."""
    assert pipeline._apply_case("Te Amo Argentina", "lower") == "te amo Argentina"


def test_lower_titlecased_preserves_country_with_accent():
    assert pipeline._apply_case("Viva México", "lower") == "viva México"


def test_lower_natural_case_still_preserves_interior_caps():
    """A naturally sentence-cased line (Whisper) is NOT treated as a
    title-cased source — interior proper nouns survive untouched, including
    operator hand-capitalizations."""
    assert pipeline._apply_case("brilla el Sol", "lower") == "brilla el Sol"


def test_lower_single_titlecased_word():
    assert pipeline._apply_case("Caminando", "lower") == "caminando"
