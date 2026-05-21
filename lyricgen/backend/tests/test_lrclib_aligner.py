"""Unit tests for the LRCLib-plain → Whisper-words aligner.

These tests use synthetic word streams (no real Whisper call) so they
exercise the alignment logic without the latency or non-determinism of
running a model.
"""

import os

from lrclib_aligner import (
    align_lrclib_to_whisper,
    split_long_segments_by_words,
    _is_hallucination,
    _normalize,
    _env_float,
    _env_int,
)


def _make_segment(text: str, words: list[tuple[str, float, float]]) -> dict:
    """Build a Whisper-shape segment from (word, start, end) tuples."""
    return {
        "text": text,
        "start": words[0][1] if words else 0.0,
        "end": words[-1][2] if words else 0.0,
        "words": [{"word": w, "start": s, "end": e} for w, s, e in words],
    }


def test_aligner_keeps_lrclib_line_structure_with_whisper_timing():
    """Whisper merges two short LRCLib lines into one segment; aligner
    should split that segment back into two lines using word timing."""
    plain = "hola mundo\nadios mundo"
    # Whisper produced ONE long segment containing both lines
    whisper = [
        _make_segment(
            "hola mundo adios mundo",
            [("hola", 1.0, 1.4), ("mundo", 1.4, 2.0),
             ("adios", 2.5, 2.9), ("mundo", 2.9, 3.5)],
        ),
    ]
    out = align_lrclib_to_whisper(plain, whisper, min_ratio=0.6)
    assert len(out) == 2
    assert out[0]["text"] == "hola mundo"
    assert out[1]["text"] == "adios mundo"
    # First line should start at the first matched word; not at the
    # segment's overall start.
    assert out[0]["start"] == 1.0
    assert out[1]["start"] == 2.5


def test_aligner_skips_lines_whisper_did_not_transcribe():
    """If Whisper missed a line entirely, the aligner should skip it
    rather than guess a timestamp."""
    plain = "primera linea\nsegunda linea\ntercera linea"
    # Whisper got first and third but missed the second
    whisper = [
        _make_segment("primera linea",
                      [("primera", 1.0, 1.5), ("linea", 1.5, 2.0)]),
        _make_segment("tercera linea",
                      [("tercera", 10.0, 10.5), ("linea", 10.5, 11.0)]),
    ]
    out = align_lrclib_to_whisper(plain, whisper, min_ratio=0.7)
    assert len(out) == 2
    assert [o["text"] for o in out] == ["primera linea", "tercera linea"]


def test_aligner_filters_youtube_outro_hallucinations():
    """Whisper turbo invents '¡Suscríbete al canal!' on instrumental
    intros. The aligner must drop those segments so they can't poison
    the cursor advancement."""
    plain = "vamos a cantar"
    whisper = [
        _make_segment("¡Suscríbete al canal!",
                      [("Suscríbete", 0.5, 1.0), ("al", 1.0, 1.2),
                       ("canal", 1.2, 1.7)]),
        _make_segment("vamos a cantar",
                      [("vamos", 2.0, 2.3), ("a", 2.3, 2.4),
                       ("cantar", 2.4, 3.0)]),
    ]
    out = align_lrclib_to_whisper(plain, whisper, min_ratio=0.7)
    assert len(out) == 1
    assert out[0]["text"] == "vamos a cantar"
    assert out[0]["start"] == 2.0


def test_aligner_preserves_song_order_when_lines_repeat():
    """A chorus line that repeats should match to its first occurrence,
    then the cursor must advance so the second LRCLib instance matches
    the second Whisper occurrence — not the same one twice."""
    plain = "estribillo aqui\notra cosa\nestribillo aqui"
    whisper = [
        _make_segment("estribillo aqui",
                      [("estribillo", 1.0, 1.5), ("aqui", 1.5, 2.0)]),
        _make_segment("otra cosa",
                      [("otra", 3.0, 3.3), ("cosa", 3.3, 3.7)]),
        _make_segment("estribillo aqui",
                      [("estribillo", 5.0, 5.5), ("aqui", 5.5, 6.0)]),
    ]
    out = align_lrclib_to_whisper(plain, whisper, min_ratio=0.7)
    assert len(out) == 3
    assert out[0]["start"] == 1.0
    assert out[2]["start"] == 5.0


def test_aligner_returns_empty_on_empty_input():
    assert align_lrclib_to_whisper("", []) == []
    assert align_lrclib_to_whisper("texto", []) == []
    assert align_lrclib_to_whisper("", [_make_segment("foo", [("foo", 0, 1)])]) == []


def test_is_hallucination_catches_known_outros():
    assert _is_hallucination("¡Suscríbete al canal!")
    assert _is_hallucination("Suscribete")
    assert _is_hallucination("Música")
    assert _is_hallucination("Gracias por ver")
    assert _is_hallucination("Subtítulos creados por la comunidad")
    assert not _is_hallucination("vamos a cantar esta canción")
    assert not _is_hallucination("oh oh oh")


def test_normalize_strips_punctuation_and_lowercases():
    assert _normalize("Hola, mundo!") == "hola mundo"
    assert _normalize("¡QUÉ?") == "qué"
    assert _normalize("   hola   ") == "hola"


# --- Gemini-style reference (multi-line, accents) ------------------------

def test_aligner_works_with_gemini_style_reference():
    """The aligner is source-agnostic — same logic re-buckets a Gemini
    plain-lyrics block (accents, short interjection lines) against Whisper
    words. This is the path that was missing for live recordings."""
    plain = "Perro amor explota\nEn la avenida\nOh oh oh"
    whisper = [
        _make_segment(
            "perro amor explota en la avenida oh oh oh",
            [("perro", 1.0, 1.3), ("amor", 1.3, 1.6), ("explota", 1.6, 2.2),
             ("en", 3.0, 3.1), ("la", 3.1, 3.2), ("avenida", 3.2, 3.9),
             ("oh", 4.5, 4.7), ("oh", 4.7, 4.9), ("oh", 4.9, 5.1)],
        ),
    ]
    out = align_lrclib_to_whisper(plain, whisper, min_ratio=0.6)
    assert [o["text"] for o in out] == ["Perro amor explota", "En la avenida", "Oh oh oh"]
    assert out[0]["start"] == 1.0
    assert out[1]["start"] == 3.0
    assert out[2]["end"] == 5.1


# --- Env-tunable knobs ----------------------------------------------------

def test_env_float_and_int_parse_and_default():
    assert _env_float("NOPE_MISSING_VAR_X", 0.72) == 0.72
    assert _env_int("NOPE_MISSING_VAR_Y", 25) == 25


def test_aligner_reads_min_ratio_from_env_when_not_passed(monkeypatch):
    """When min_ratio isn't passed, the threshold comes from
    LRCLIB_ALIGNER_MIN_RATIO. Exact-match line (ratio 1.0) aligns under a
    low env threshold and is rejected when the env demands an impossible
    ratio — proving the env value is what's used."""
    plain = "hola mundo"
    whisper = [
        _make_segment("hola mundo",
                      [("hola", 1.0, 1.5), ("mundo", 1.5, 2.0)]),
    ]
    monkeypatch.setenv("LRCLIB_ALIGNER_MIN_RATIO", "0.5")
    assert len(align_lrclib_to_whisper(plain, whisper)) == 1
    monkeypatch.setenv("LRCLIB_ALIGNER_MIN_RATIO", "1.5")  # impossible
    assert align_lrclib_to_whisper(plain, whisper) == []


# --- Length-split safety net ---------------------------------------------

def test_split_long_segment_at_largest_gap():
    """An over-long segment with word timings splits at the biggest
    inter-word silence; pieces preserve order, edge timing, and text."""
    seg = _make_segment(
        "uno dos tres cuatro cinco seis siete ocho",
        [("uno", 0.0, 0.3), ("dos", 0.3, 0.6), ("tres", 0.6, 0.9),
         ("cuatro", 0.9, 1.2),
         ("cinco", 3.0, 3.3),   # 1.8 s gap before "cinco" → cut here
         ("seis", 3.3, 3.6), ("siete", 3.6, 3.9), ("ocho", 3.9, 4.2)],
    )
    out = split_long_segments_by_words([seg], max_words=4, max_chars=10)
    assert len(out) >= 2
    # Edge timing preserved across the split.
    assert out[0]["start"] == 0.0
    assert out[-1]["end"] == 4.2
    # The big gap boundary shows up: some piece ends at 1.2 and the next
    # starts at 3.0.
    assert any(abs(o["end"] - 1.2) < 1e-9 for o in out)
    assert any(abs(o["start"] - 3.0) < 1e-9 for o in out)
    # Full text preserved in order, no words leaked into output.
    assert " ".join(o["text"] for o in out) == "uno dos tres cuatro cinco seis siete ocho"
    assert all("words" not in o for o in out)


def test_split_leaves_short_segments_untouched_and_strips_words():
    seg = _make_segment("hola mundo",
                        [("hola", 0.0, 0.5), ("mundo", 0.5, 1.0)])
    out = split_long_segments_by_words([seg], max_words=12, max_chars=42)
    assert out == [{"start": 0.0, "end": 1.0, "text": "hola mundo"}]


def test_split_passes_through_segments_without_words():
    out = split_long_segments_by_words(
        [{"start": 1.0, "end": 2.0, "text": "sin words"}]
    )
    assert out == [{"start": 1.0, "end": 2.0, "text": "sin words"}]
