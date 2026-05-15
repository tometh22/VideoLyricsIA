"""Unit tests for the LRCLib-plain → Whisper-words aligner.

These tests use synthetic word streams (no real Whisper call) so they
exercise the alignment logic without the latency or non-determinism of
running a model.
"""

from lrclib_aligner import (
    align_lrclib_to_whisper,
    _is_hallucination,
    _normalize,
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
