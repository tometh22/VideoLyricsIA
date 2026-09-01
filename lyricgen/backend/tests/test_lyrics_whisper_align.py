"""Tests para lyrics_whisper_align.

Cobertura del DP align + build_segments puros (sin llamar Whisper API).
La función pública `whisper_word_align` requiere OPENAI_API_KEY y un
audio real, así que se testea end-to-end en `/tmp/test_whisper_aligner.py`
con audio de Downloads (no aquí, no en CI sin secret).
"""
from __future__ import annotations

import sys
import os
import inspect

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import pytest  # noqa: E402

from lyrics_whisper_align import (  # noqa: E402
    _dp_align_tokens,
    _tokens_with_line,
    _build_segments,
    _lev_similarity,
    _map_provider_words,
    _raw_provider_words,
    whisper_word_align,
    MIN_ANCHOR_RATIO,
    MIN_SEG_DUR_S,
)


def test_provider_word_mapping_preserves_empty_completion_and_sdk_objects():
    assert _map_provider_words([]) == []
    sdk_word = type("Word", (), {
        "word": "hola", "start": 1.0, "end": 1.4,
    })()
    assert _map_provider_words([sdk_word, {
        "word": "mundo", "start": 1.5, "end": 2.0,
    }]) == [
        {"word": "hola", "start": 1.0, "end": 1.4},
        {"word": "mundo", "start": 1.5, "end": 2.0},
    ]


def test_raw_provider_words_preserves_opaque_rows_before_mapping():
    class OpaqueWord:
        def model_dump(self):
            raise ValueError("malformed SDK row")

        def __getattr__(self, _name):
            raise AttributeError

        def __str__(self):
            raise RuntimeError("SDK object cannot be stringified")

    raw = _raw_provider_words([
        {"word": "hola", "start": 1.0, "end": 1.4},
        OpaqueWord(),
    ])

    assert raw == [
        {"word": "hola", "start": 1.0, "end": 1.4},
        {"raw": "<opaque-provider-value-OpaqueWord>"},
    ]


def test_alignment_records_raw_words_before_selection_mapping():
    source = inspect.getsource(whisper_word_align)
    raw_index = source.index("raw_word_dicts = _raw_provider_words(words)")
    record_index = source.index("record_completed(", raw_index)
    map_index = source.index("word_dicts = _map_provider_words(words)")
    assert raw_index < record_index < map_index


# ──────────────────────────────────────────────────────────────────────
# _lev_similarity
# ──────────────────────────────────────────────────────────────────────

def test_lev_similarity_exact_match():
    assert _lev_similarity("hola", "hola") == 1.0


def test_lev_similarity_prefix():
    # decía / decías (conjugation tail).
    assert _lev_similarity("decia", "decias") == 0.5
    assert _lev_similarity("decias", "decia") == 0.5


def test_lev_similarity_too_short_prefix():
    # 2-char prefixes don't count (too noisy).
    assert _lev_similarity("a", "ab") == 0.0
    assert _lev_similarity("am", "amor") == 0.0


def test_lev_similarity_unrelated():
    assert _lev_similarity("hola", "adios") == 0.0


# ──────────────────────────────────────────────────────────────────────
# _tokens_with_line
# ──────────────────────────────────────────────────────────────────────

def test_tokens_with_line_basic():
    lines = ["Hola mundo", "qué tal"]
    toks = _tokens_with_line(lines)
    assert toks == [(0, "hola"), (0, "mundo"), (1, "qué"), (1, "tal")]


def test_tokens_with_line_strips_punctuation_and_numbers():
    lines = ['Llamame al "6380465".', "Y empecé."]
    toks = _tokens_with_line(lines)
    assert toks == [
        (0, "llamame"), (0, "al"), (0, "6380465"),
        (1, "y"), (1, "empecé"),
    ]


# ──────────────────────────────────────────────────────────────────────
# _dp_align_tokens
# ──────────────────────────────────────────────────────────────────────

def test_dp_align_perfect_match():
    cleaned = [(0, "hola"), (0, "mundo"), (1, "adiós"), (1, "amigo")]
    whisper = [
        {"word": "hola", "start": 0.0, "end": 0.5},
        {"word": "mundo", "start": 0.5, "end": 1.0},
        {"word": "adiós", "start": 1.5, "end": 2.0},
        {"word": "amigo", "start": 2.0, "end": 2.5},
    ]
    result = _dp_align_tokens(cleaned, whisper)
    assert result == [0, 1, 2, 3]


def test_dp_align_skip_extra_whisper_word():
    """Whisper transcribed an extra interjection that's not in cleaned."""
    cleaned = [(0, "hola"), (0, "mundo")]
    whisper = [
        {"word": "hola", "start": 0.0, "end": 0.5},
        {"word": "eh", "start": 0.5, "end": 0.7},  # filler not in cleaned
        {"word": "mundo", "start": 0.7, "end": 1.2},
    ]
    result = _dp_align_tokens(cleaned, whisper)
    assert result == [0, 2]


def test_dp_align_missing_cleaned_token():
    """Cleaned has a word that Whisper didn't transcribe."""
    cleaned = [(0, "hola"), (0, "tal"), (0, "vez"), (0, "mundo")]
    whisper = [
        {"word": "hola", "start": 0.0, "end": 0.5},
        # "tal" missing — Whisper didn't transcribe
        # "vez" missing — Whisper didn't transcribe
        {"word": "mundo", "start": 1.0, "end": 1.5},
    ]
    result = _dp_align_tokens(cleaned, whisper)
    # First and last anchored, middle 2 unanchored.
    assert result[0] == 0
    assert result[-1] == 1
    assert result[1] == -1
    assert result[2] == -1


def test_dp_align_repeated_chorus_matches_first_occurrences():
    """Cleaned has chorus repeated; whisper has same words. DP should
    pair them in order (1st cleaned chorus → 1st whisper chorus, etc.)."""
    cleaned = [
        (0, "llamame"), (0, "al"),
        (1, "llamame"), (1, "al"),
        (2, "llamame"), (2, "al"),
    ]
    whisper = [
        {"word": "llamame", "start": 10.0, "end": 10.5},
        {"word": "al",      "start": 10.5, "end": 10.8},
        {"word": "llamame", "start": 15.0, "end": 15.5},
        {"word": "al",      "start": 15.5, "end": 15.8},
        {"word": "llamame", "start": 20.0, "end": 20.5},
        {"word": "al",      "start": 20.5, "end": 20.8},
    ]
    result = _dp_align_tokens(cleaned, whisper)
    assert result == [0, 1, 2, 3, 4, 5]


def test_dp_align_empty_inputs():
    assert _dp_align_tokens([], []) == []
    assert _dp_align_tokens([(0, "x")], []) == [-1]
    assert _dp_align_tokens([], [{"word": "x", "start": 0.0, "end": 0.5}]) == []


# ──────────────────────────────────────────────────────────────────────
# _build_segments
# ──────────────────────────────────────────────────────────────────────

def test_build_segments_all_anchored():
    cleaned_lines = ["primera", "segunda"]
    cleaned_tokens = [(0, "primera"), (1, "segunda")]
    cleaned_to_whisper = [0, 1]
    whisper = [
        {"word": "primera", "start": 5.0, "end": 5.5},
        {"word": "segunda", "start": 10.0, "end": 10.5},
    ]
    segs = _build_segments(cleaned_lines, cleaned_tokens, cleaned_to_whisper, whisper, 30.0)
    assert len(segs) == 2
    assert segs[0]["start"] == 5.0
    assert segs[1]["start"] == 10.0
    assert segs[0]["end"] < segs[1]["start"]


def test_build_segments_interpolates_middle():
    """Cleaned line between anchors gets interpolated."""
    cleaned_lines = ["primera", "puente", "tercera"]
    cleaned_tokens = [(0, "primera"), (1, "puente"), (2, "tercera")]
    cleaned_to_whisper = [0, -1, 1]  # middle unanchored
    whisper = [
        {"word": "primera", "start": 5.0, "end": 5.5},
        {"word": "tercera", "start": 15.0, "end": 15.5},
    ]
    segs = _build_segments(cleaned_lines, cleaned_tokens, cleaned_to_whisper, whisper, 30.0)
    assert segs[0]["start"] == 5.0
    assert segs[2]["start"] == 15.0
    # Middle interpolates ~halfway.
    assert 8.0 < segs[1]["start"] < 12.0


def test_build_segments_interpolates_tail():
    """Lines after last anchor get tail interpolation."""
    cleaned_lines = ["anchored", "outro1", "outro2"]
    cleaned_tokens = [(0, "anchored"), (1, "outro1"), (2, "outro2")]
    cleaned_to_whisper = [0, -1, -1]
    whisper = [{"word": "anchored", "start": 5.0, "end": 5.5}]
    audio_dur = 30.0
    segs = _build_segments(cleaned_lines, cleaned_tokens, cleaned_to_whisper, whisper, audio_dur)
    assert segs[0]["start"] == 5.0
    # Outros distribute toward audio_dur.
    assert 5.0 < segs[1]["start"] < segs[2]["start"]
    assert segs[2]["start"] <= audio_dur


def test_build_segments_monotonicity_enforced():
    """Even if interpolation produces equal/backward starts, output is
    strictly monotonic with MIN_SEG_DUR_S floors."""
    cleaned_lines = ["a", "b", "c"]
    cleaned_tokens = [(0, "a"), (1, "b"), (2, "c")]
    cleaned_to_whisper = [0, 1, 2]
    # Three whisper words all at nearly the same time (artificial).
    whisper = [
        {"word": "a", "start": 5.0, "end": 5.05},
        {"word": "b", "start": 5.0, "end": 5.05},
        {"word": "c", "start": 5.0, "end": 5.05},
    ]
    segs = _build_segments(cleaned_lines, cleaned_tokens, cleaned_to_whisper, whisper, 30.0)
    for i in range(1, len(segs)):
        assert segs[i]["start"] > segs[i - 1]["start"]


def test_build_segments_returns_empty_on_zero_anchors():
    cleaned_lines = ["a", "b"]
    cleaned_tokens = [(0, "a"), (1, "b")]
    cleaned_to_whisper = [-1, -1]
    whisper = [{"word": "x", "start": 5.0, "end": 5.5}]
    segs = _build_segments(cleaned_lines, cleaned_tokens, cleaned_to_whisper, whisper, 30.0)
    assert segs == []


def test_build_segments_min_duration_floor():
    """Even when anchors are very close, each segment has at least
    MIN_SEG_DUR_S of duration."""
    cleaned_lines = ["uno", "dos"]
    cleaned_tokens = [(0, "uno"), (1, "dos")]
    cleaned_to_whisper = [0, 1]
    whisper = [
        {"word": "uno", "start": 5.0, "end": 5.05},
        {"word": "dos", "start": 5.1, "end": 5.15},
    ]
    segs = _build_segments(cleaned_lines, cleaned_tokens, cleaned_to_whisper, whisper, 30.0)
    for s in segs:
        assert s["end"] - s["start"] >= MIN_SEG_DUR_S - 1e-6


# ──────────────────────────────────────────────────────────────────────
# 2026-05-31 — fixes A/B/C for the Agus "Donde Estan Corazón" report.
# ──────────────────────────────────────────────────────────────────────


def test_build_segments_attaches_words_when_available():
    """Fix A: segments must carry words[] sourced from whisper_words so
    the editor's karaoke highlight has per-word timing instead of
    linear interpolation. Pre-fix the function discarded the 300+
    word_dicts available, leaving editor highlight wildly off when
    seg.end was tight."""
    cleaned_lines = ["uno dos", "tres cuatro"]
    cleaned_tokens = [(0, "uno"), (0, "dos"), (1, "tres"), (1, "cuatro")]
    cleaned_to_whisper = [0, 1, 2, 3]
    whisper = [
        {"word": "uno",    "start": 5.00, "end": 5.20},
        {"word": "dos",    "start": 5.30, "end": 5.50},
        {"word": "tres",   "start": 6.00, "end": 6.20},
        {"word": "cuatro", "start": 6.30, "end": 6.50},
    ]
    segs = _build_segments(cleaned_lines, cleaned_tokens, cleaned_to_whisper, whisper, 30.0)
    assert len(segs) == 2
    # Each segment now carries its two source words.
    for s in segs:
        assert "words" in s and len(s["words"]) == 2
        for w in s["words"]:
            assert {"word", "start", "end", "score"} <= set(w.keys())
            # Whisper-1 confidence placeholder: above beat_snap's
            # _RELIABLE_WORD_SCORE (0.5) so beat_snap rightly skips
            # these segments, but below typical forced-align ~0.9.
            assert 0.5 < w["score"] < 0.9


def test_build_segments_omits_words_when_none_overlap():
    """Edge case for Fix A: a segment built purely from interpolation
    (no whisper word falls in its window) gets NO `words` key. The
    editor's fallback (linear interpolation of text) takes over."""
    # Two lines, but whisper only has words around the second.
    cleaned_lines = ["intro hum", "real line"]
    cleaned_tokens = [(1, "real"), (1, "line")]
    cleaned_to_whisper = [0, 1]
    whisper = [
        {"word": "real", "start": 10.0, "end": 10.3},
        {"word": "line", "start": 10.4, "end": 10.7},
    ]
    segs = _build_segments(cleaned_lines, cleaned_tokens, cleaned_to_whisper, whisper, 30.0)
    # Line 0 is interpolated from the head heuristic; no whisper word
    # falls inside its [start, end) window → no `words` key.
    assert "words" not in segs[0]
    # Line 1 anchored on whisper words → both attached.
    assert len(segs[1].get("words") or []) == 2


def test_build_segments_warns_on_truncated_segment(caplog):
    """Fix C: a segment whose duration-per-character is below the
    perceptual floor (70 ms/char) logs a warning so future regressions
    surface in Sentry breadcrumbs. The bug Agus reported on "Donde
    Estan Corazón" job 177e8eafb473 had seg[0] at 1.45s for 36 chars
    = 40 ms/char — below floor."""
    import logging

    cleaned_lines = ["a dónde fue el pasado que no volverá"]
    cleaned_tokens = [(0, "donde"), (0, "fue")]
    cleaned_to_whisper = [0, 1]
    # Force seg dur ~= 1.5s for 36 chars → 41 ms/char (BELOW the floor).
    whisper = [
        {"word": "donde", "start": 16.78, "end": 16.90},
        {"word": "fue",   "start": 17.00, "end": 17.10},
    ]
    with caplog.at_level(logging.WARNING):
        _build_segments(cleaned_lines, cleaned_tokens, cleaned_to_whisper,
                        whisper, audio_dur=18.23)
    # The single segment's [start=16.78, end~=18.23], dur ~= 1.45s,
    # chars=36 → ~40 ms/char → MUST warn.
    assert any("truncated" in r.message and "ms/char" in r.message
               for r in caplog.records)


def test_build_segments_no_warning_on_normal_density():
    """Normal sung delivery (100-150 ms/char) must NOT warn. Pin so a
    future tightening of the floor doesn't accidentally flag good
    segments."""
    import logging

    cleaned_lines = ["a dónde fue el pasado que no volverá"]   # 36 chars
    cleaned_tokens = [(0, "dónde"), (0, "fue")]
    cleaned_to_whisper = [0, 1]
    # Seg dur ~= 4.5s for 36 chars → 125 ms/char (normal sung pace).
    whisper = [
        {"word": "dónde", "start": 16.78, "end": 16.95},
        {"word": "fue",   "start": 17.10, "end": 17.30},
    ]
    with caplog_collector() as records:
        _build_segments(cleaned_lines, cleaned_tokens, cleaned_to_whisper,
                        whisper, audio_dur=21.28)
    assert not any("truncated" in r for r in records), \
        "normal density segment must NOT warn"


import contextlib
@contextlib.contextmanager
def caplog_collector():
    """Tiny helper: capture warning messages without depending on
    pytest's caplog (some tests use plain context managers above)."""
    import logging
    out: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda r: out.append(r.getMessage())
    handler.setLevel(logging.WARNING)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield out
    finally:
        root.removeHandler(handler)


# ──────────────────────────────────────────────────────────────────────
# Integration smoke: token+align+build full path on synthetic data.
# ──────────────────────────────────────────────────────────────────────

def test_integration_638_like_synthetic():
    """Mimics the "638" structure: cleaned has 7 lines (intro + chorus
    + outro repeated) vs whisper has 12 word stamps covering them."""
    cleaned_lines = [
        "Tenía tantas ganas de volverte a ver",
        "Y empecé",
        "Y empecé",
        "Y empecé a pensar",
        "Llamame al 638",   # first occurrence
        "Llamame al 638",   # outro repeat
        "Llamame al 638",   # outro repeat
    ]
    cleaned_tokens = _tokens_with_line(cleaned_lines)

    # Synthetic whisper: each "Y empecé" sung at its own time, outro
    # also has each "Llamame al 638" at its own time.
    whisper = [
        {"word": "Tenía",   "start": 21.0, "end": 21.5},
        {"word": "tantas",  "start": 21.5, "end": 22.0},
        {"word": "ganas",   "start": 22.0, "end": 22.5},
        {"word": "de",      "start": 22.5, "end": 22.7},
        {"word": "volverte","start": 22.7, "end": 23.2},
        {"word": "a",       "start": 23.2, "end": 23.4},
        {"word": "ver",     "start": 23.4, "end": 23.8},
        {"word": "Y",       "start": 31.0, "end": 31.2},
        {"word": "empecé",  "start": 31.2, "end": 31.8},
        {"word": "Y",       "start": 32.0, "end": 32.2},
        {"word": "empecé",  "start": 32.2, "end": 32.8},
        {"word": "Y",       "start": 33.0, "end": 33.2},
        {"word": "empecé",  "start": 33.2, "end": 33.8},
        {"word": "a",       "start": 33.8, "end": 34.0},
        {"word": "pensar",  "start": 34.0, "end": 34.5},
        {"word": "Llamame", "start": 50.0, "end": 50.5},
        {"word": "al",      "start": 50.5, "end": 50.7},
        {"word": "638",     "start": 50.7, "end": 51.2},
        {"word": "Llamame", "start": 130.0, "end": 130.5},
        {"word": "al",      "start": 130.5, "end": 130.7},
        {"word": "638",     "start": 130.7, "end": 131.2},
        {"word": "Llamame", "start": 140.0, "end": 140.5},
        {"word": "al",      "start": 140.5, "end": 140.7},
        {"word": "638",     "start": 140.7, "end": 141.2},
    ]
    cleaned_to_whisper = _dp_align_tokens(cleaned_tokens, whisper)
    segs = _build_segments(cleaned_lines, cleaned_tokens, cleaned_to_whisper, whisper, 180.0)

    assert len(segs) == 7
    # Verso 1 line at ~21s.
    assert abs(segs[0]["start"] - 21.0) < 0.5
    # Three "Y empecé" lines anchored at their distinct whisper times.
    assert abs(segs[1]["start"] - 31.0) < 0.5
    assert abs(segs[2]["start"] - 32.0) < 0.5
    assert abs(segs[3]["start"] - 33.0) < 0.5
    # Three "Llamame al 638" lines anchored at their distinct outro times.
    assert abs(segs[4]["start"] - 50.0) < 0.5
    assert abs(segs[5]["start"] - 130.0) < 0.5
    assert abs(segs[6]["start"] - 140.0) < 0.5
    # Strict monotonicity.
    for i in range(1, len(segs)):
        assert segs[i]["start"] > segs[i - 1]["start"]
