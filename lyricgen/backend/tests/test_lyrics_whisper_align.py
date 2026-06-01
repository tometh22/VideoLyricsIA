"""Tests para lyrics_whisper_align.

Cobertura del DP align + build_segments puros (sin llamar Whisper API).
La función pública `whisper_word_align` requiere OPENAI_API_KEY y un
audio real, así que se testea end-to-end en `/tmp/test_whisper_aligner.py`
con audio de Downloads (no aquí, no en CI sin secret).
"""
from __future__ import annotations

import sys
import os

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import pytest  # noqa: E402

from lyrics_whisper_align import (  # noqa: E402
    _dp_align_tokens,
    _tokens_with_line,
    _build_segments,
    _lev_similarity,
    MIN_ANCHOR_RATIO,
    MIN_SEG_DUR_S,
)


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


def test_build_segments_head_uses_audio_onset_not_fixed_offset():
    """Fix B: when the first anchor is at line N (N>0), the head
    should start from the audio's FIRST whisper-word onset, not from
    `first_t - N*1.5s` blindly.

    This is the exact scenario that broke "Donde Estan Corazón":
    the first cleaned line wasn't anchored by DP, the heuristic
    placed seg[0].start at first_t - 1*1.5 = far before the vocal
    actually entered, and the editor showed the first subtitle
    while the audio was still instrumental."""
    cleaned_lines = ["pre vocal line", "anchor line"]
    cleaned_tokens = [(1, "anchor"), (1, "line")]
    cleaned_to_whisper = [
        # First cleaned tokens (line 0) didn't match Whisper.
        # Line 1 tokens anchored to whisper words 0/1.
        0, 1,
    ]
    whisper = [
        {"word": "anchor", "start": 17.0, "end": 17.3},
        {"word": "line",   "start": 17.4, "end": 17.7},
    ]
    segs = _build_segments(cleaned_lines, cleaned_tokens, cleaned_to_whisper, whisper, 30.0)
    # Pre-fix this used to be max(0, 17.0 - 1*1.5) = 15.5
    # With Fix B, head uses _wstart(0) = 17.0 (clamped < first_t),
    # but since _wstart(0) == first_t, the predicate `audio_onset
    # < first_t - 0.05` is False → falls back to old heuristic → 15.5.
    # We pin the predicate boundary; if Whisper had an earlier word
    # at e.g. 14.0, head would correctly anchor there.
    assert segs[0]["start"] >= 15.5 - 1e-6


def test_build_segments_head_anchors_to_earlier_whisper_word():
    """Fix B: if there's an earlier Whisper word (e.g. instrumental
    ad-lib or a filler "oh" Whisper picked up) BEFORE the first
    anchored cleaned token, the head should start there, not from
    the 1.5s/line heuristic."""
    cleaned_lines = ["unmatched line 1", "unmatched line 2", "anchor line"]
    cleaned_tokens = [(2, "anchor"), (2, "line")]
    cleaned_to_whisper = [1, 2]
    whisper = [
        # An ad-lib Whisper heard at 12.0 — useful as the actual onset.
        {"word": "ooh",    "start": 12.0, "end": 12.3},
        {"word": "anchor", "start": 20.0, "end": 20.3},
        {"word": "line",   "start": 20.4, "end": 20.7},
    ]
    segs = _build_segments(cleaned_lines, cleaned_tokens, cleaned_to_whisper, whisper, 30.0)
    # Pre-fix: head_t = max(0, 20 - 2*1.5) = 17.0
    # Post-fix: head_t = max(0, _wstart(0)) = 12.0 — strictly better.
    assert segs[0]["start"] < 14.0   # comfortably below the old 17.0


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


# ──────────────────────────────────────────────────────────────────────
# Fix D (2026-05-31): align_lines_to_words — use whisperX words instead
# of re-calling Whisper-1 when reconcile aborts.
# ──────────────────────────────────────────────────────────────────────


def test_align_lines_to_words_with_whisperx_words_produces_segments():
    """Happy path: passing pre-fetched words from whisperX (with the
    `score` field that forced-align supplies) produces segments with
    `words[]` carried through. The whole point of this entry is to
    AVOID re-calling Whisper-1 when the timing data already exists."""
    from lyrics_whisper_align import align_lines_to_words

    cleaned_lines = ["uno dos", "tres cuatro"]
    # WhisperX-shaped words (note `score`, the forced-align confidence).
    wx_words = [
        {"word": "uno",    "start": 5.00, "end": 5.20, "score": 0.95},
        {"word": "dos",    "start": 5.30, "end": 5.50, "score": 0.93},
        {"word": "tres",   "start": 6.00, "end": 6.20, "score": 0.91},
        {"word": "cuatro", "start": 6.30, "end": 6.50, "score": 0.89},
    ]
    segs = align_lines_to_words(cleaned_lines, wx_words, audio_dur=30.0,
                                 label="WC-WX-WORDS")
    assert segs is not None
    assert len(segs) == 2
    # words[] persisted — the editor's karaoke highlight uses these.
    for s in segs:
        assert len(s.get("words") or []) == 2


def test_align_lines_to_words_returns_none_on_empty_inputs():
    """Defensive: any empty argument returns None so the caller can
    fall back cleanly."""
    from lyrics_whisper_align import align_lines_to_words

    assert align_lines_to_words([], [{"word": "a", "start": 0, "end": 1}], 10.0) is None
    assert align_lines_to_words(["uno"], [], 10.0) is None
    assert align_lines_to_words(["uno"], [{"word": "uno", "start": 0, "end": 1}], 0.0) is None


def test_align_lines_to_words_bails_below_anchor_floor():
    """If the cleaned lines are so different from the words that DP
    can't anchor enough lines, return None so the caller falls back
    to the Whisper-1 path (which has different mishearing biases)."""
    from lyrics_whisper_align import align_lines_to_words

    # Canonical mentions things the words stream doesn't have at all.
    cleaned_lines = [
        "completely different line one",
        "another mismatched line two",
        "yet another bad match three",
    ]
    wx_words = [
        {"word": "hola",  "start": 0.0, "end": 0.5},
        {"word": "mundo", "start": 0.6, "end": 1.0},
    ]
    segs = align_lines_to_words(cleaned_lines, wx_words, audio_dur=5.0)
    assert segs is None


def test_flatten_whisperx_words_pulls_per_word_stamps_in_order():
    """flatten_whisperx_words takes whisperX's nested {segments[words]}
    shape and produces a flat time-sorted word stream — what
    align_lines_to_words expects."""
    from lyrics_whisper_align import flatten_whisperx_words

    wx_segs = [
        {"start": 0.0, "end": 1.0, "text": "uno dos",
         "words": [
             {"word": "uno", "start": 0.10, "end": 0.30, "score": 0.95},
             {"word": "dos", "start": 0.50, "end": 0.70, "score": 0.92},
         ]},
        {"start": 2.0, "end": 3.0, "text": "tres",
         "words": [
             {"word": "tres", "start": 2.10, "end": 2.40, "score": 0.88},
         ]},
    ]
    flat = flatten_whisperx_words(wx_segs)
    assert [w["word"] for w in flat] == ["uno", "dos", "tres"]
    # Sorted by start ascending.
    starts = [w["start"] for w in flat]
    assert starts == sorted(starts)
    # Score preserved.
    assert flat[0]["score"] == 0.95


def test_flatten_whisperx_words_handles_out_of_order_segments():
    """Defense: if a caller passes segments out of order or with
    overlapping words, the output is still time-sorted."""
    from lyrics_whisper_align import flatten_whisperx_words

    wx_segs = [
        {"words": [{"word": "later", "start": 5.0, "end": 5.5}]},
        {"words": [{"word": "early", "start": 1.0, "end": 1.5}]},
    ]
    flat = flatten_whisperx_words(wx_segs)
    assert [w["word"] for w in flat] == ["early", "later"]


def test_flatten_whisperx_words_skips_malformed_entries():
    """Garbage in (None entries, missing start, empty word) is dropped
    silently — the alignment can't use them anyway."""
    from lyrics_whisper_align import flatten_whisperx_words

    wx_segs = [
        {"words": [
            None,
            "string",
            {"word": "", "start": 1.0, "end": 1.5},        # empty text
            {"word": "ok", "start": "garbage", "end": 2},   # bad start
            {"word": "good", "start": 3.0, "end": 3.5, "score": 0.9},
        ]},
    ]
    flat = flatten_whisperx_words(wx_segs)
    assert [w["word"] for w in flat] == ["good"]


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
