"""Trailing-period stripper — UMG's most repeated change request.

Six explicit requests across three months, most recently three days before
this was written: *"sacar los puntos finales de todas las frases"*. Measured
on `editor_documents`: 214 of 586 lines arrive with a trailing period, only 35
of 593 survive delivery — operators were stripping 83.6% by hand.

The scope is deliberately narrow, and these tests are what keep it narrow: a
stripper that also ate `?`, `!` or ellipsis would create a new complaint while
fixing the old one.
"""

import pytest

from lyrics_format import strip_trailing_periods


def _r(*texts):
    return {"segments": [{"text": t, "start": i, "end": i + 1}
                         for i, t in enumerate(texts)]}


def _texts(result):
    return [s["text"] for s in result["segments"]]


# ---------------------------------------------------------------------------
# What it must strip
# ---------------------------------------------------------------------------

def test_strips_a_single_trailing_period():
    assert _texts(strip_trailing_periods(_r("Dicen que soy lo peor."))) == \
        ["Dicen que soy lo peor"]


def test_strips_period_before_trailing_whitespace():
    assert _texts(strip_trailing_periods(_r("La argentinidad al palo.  "))) == \
        ["La argentinidad al palo"]


def test_strips_per_physical_line_in_a_multiline_card():
    """Each on-screen line is a 'frase' to the client. No multi-line cards
    exist in the current data, but the render path allows them."""
    out = _texts(strip_trailing_periods(_r("Primera frase.\nSegunda frase.")))
    assert out == ["Primera frase\nSegunda frase"]


# ---------------------------------------------------------------------------
# What it must NOT touch — each of these would be a new complaint
# ---------------------------------------------------------------------------

def test_keeps_question_and_exclamation_marks():
    """55 lines end in '?' and 27 in '!'. UMG never asked for those, and the
    Spanish formatting prompt adds the opening ¿/¡ on purpose."""
    out = _texts(strip_trailing_periods(_r("¿Quién te mira?", "¡Vamos!")))
    assert out == ["¿Quién te mira?", "¡Vamos!"]


def test_keeps_ellipsis():
    """14 lines legitimately trail off. Removing one dot of '...' would leave
    a broken '..' — worse than the original problem."""
    out = _texts(strip_trailing_periods(_r("Y sigue el tren...", "Se aleja..")))
    assert out == ["Y sigue el tren...", "Se aleja.."]


def test_keeps_unicode_ellipsis_character():
    assert _texts(strip_trailing_periods(_r("Se desvanece…"))) == ["Se desvanece…"]


def test_keeps_internal_punctuation():
    """Only the very end of the line. Internal commas and periods stay."""
    out = _texts(strip_trailing_periods(
        _r("No hay nadie más que vos y yo, nadie más.")))
    assert out == ["No hay nadie más que vos y yo, nadie más"]


def test_keeps_other_trailing_punctuation():
    out = _texts(strip_trailing_periods(_r("Escuchá:", "Y entonces,")))
    assert out == ["Escuchá:", "Y entonces,"]


# ---------------------------------------------------------------------------
# Robustness — this runs on every transcription
# ---------------------------------------------------------------------------

def test_preserves_all_other_segment_fields():
    """Timings, word arrays and the editor's line identity must survive —
    losing `words` would break the timing editor."""
    seg = {"text": "Hola.", "start": 1.5, "end": 3.25, "_id": 7,
           "words": [{"word": "Hola", "start": 1.5, "end": 3.0}],
           "locked": True}
    out = strip_trailing_periods({"segments": [seg]})["segments"][0]
    assert out["text"] == "Hola"
    assert out["start"] == 1.5 and out["end"] == 3.25
    assert out["_id"] == 7 and out["locked"] is True
    assert out["words"] == seg["words"]


def test_does_not_mutate_the_input():
    original = _r("Con punto.")
    strip_trailing_periods(original)
    assert original["segments"][0]["text"] == "Con punto."


def test_no_change_returns_input_untouched():
    r = _r("Sin punto")
    assert strip_trailing_periods(r) is r


@pytest.mark.parametrize("bad", [None, {}, {"segments": []},
                                 {"segments": [{"text": None}]},
                                 {"segments": [{"text": ""}]},
                                 {"segments": [{}]}])
def test_degenerate_inputs_do_not_raise(bad):
    """Runs inside the transcription worker on every job — it must never be
    the thing that fails a transcription."""
    strip_trailing_periods(bad)


def test_line_that_is_only_a_period():
    assert _texts(strip_trailing_periods(_r("."))) == [""]


def test_can_be_disabled_by_env(monkeypatch):
    monkeypatch.setenv("LYRICS_STRIP_TRAILING_DOT", "0")
    assert _texts(strip_trailing_periods(_r("Con punto."))) == ["Con punto."]


# ---------------------------------------------------------------------------
# Against the real distribution
# ---------------------------------------------------------------------------

def test_matches_the_measured_production_distribution():
    """Mirrors the real jul-2026 mix from `editor_documents`: 214 of 586 lines
    end in '.', of which 14 are ellipses that must survive. Expected result:
    200 stripped, everything else byte-identical."""
    lines = (
        ["Frase con punto."] * 200          # the target
        + ["Se aleja..."] * 14              # ellipsis, must survive
        + ["¿Pregunta?"] * 55
        + ["¡Exclamación!"] * 27
        + ["Sin puntuacion"] * 290
    )
    out = _texts(strip_trailing_periods(_r(*lines)))

    ends_in_single_dot = sum(
        1 for t in out if t.rstrip().endswith(".") and not t.rstrip().endswith(".."))
    assert ends_in_single_dot == 0, "no debe quedar ningún punto final"
    assert sum(1 for t in out if t.endswith("...")) == 14
    assert sum(1 for t in out if t.endswith("?")) == 55
    assert sum(1 for t in out if t.endswith("!")) == 27
    # Only the 200 targets changed.
    assert sum(1 for a, b in zip(lines, out) if a != b) == 200
