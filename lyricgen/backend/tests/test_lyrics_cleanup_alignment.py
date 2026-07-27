"""Tests para lyrics_cleanup_alignment.align_cleaned_against_synced.

Regresión para incidente 2026-05-26 "638 / Viejas Locas" (operador reportó
5 líneas amontonadas en 1:15.5 tras activar Gemini cleanup).

Categorías:
  - happy path: cleanup expandió, alineación interpola líneas nuevas
  - all-matched: cleanup no agregó líneas, todas tienen anchor
  - all-new: ningún line matchea (devuelve None — caller fallback)
  - repeated chorus: cleanup duplicó coros, anchors van a primera ocurrencia
  - reordered cleanup: cleanup invirtió 2 líneas (edge case, no debería pasar
    pero la función no debe explotar)
  - monotonicidad: starts estrictamente crecientes
  - inputs degenerados: vacíos, audio_dur=0, synced vacío
"""
from __future__ import annotations

import sys
import os

# Make backend importable.
BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import pytest  # noqa: E402

from lyrics_cleanup_alignment import (  # noqa: E402
    align_cleaned_against_synced,
    DEFAULT_MATCH_THRESHOLD,
    MIN_SEG_DUR_S,
)


# ──────────────────────────────────────────────────────────────────────
# Happy path: the "638" reproduction.
# ──────────────────────────────────────────────────────────────────────

def test_638_repro_no_pile_up():
    """Reproduces the operator-reported pile-up: lrclib has 4 lines synced
    covering 0-1 min; cleanup expanded to 7 lines (added 2 puentes + 1
    outro). Output must have 7 distinct starts, no pile-up at end of audio."""
    synced_pairs = [
        (10.0, "Tenía tantas ganas de volverte a ver"),
        (15.0, "Y empecé a pensar que tenía en un cajón"),
        (30.0, "Agarré el teléfono y me puse a marcar"),
        (40.0, "Llamame al 638"),
    ]
    cleaned_lines = [
        "Tenía tantas ganas de volverte a ver",
        "Y empecé",                                # NEW puente
        "Y empecé",                                # NEW puente repeat
        "Y empecé a pensar que tenía en un cajón",
        "Agarré el teléfono y me puse a marcar",
        "Llamame al 638",
        "Llamame al 638",                          # NEW outro repeat
    ]
    audio_dur = 60.0

    segments = align_cleaned_against_synced(cleaned_lines, synced_pairs, audio_dur)

    assert segments is not None
    assert len(segments) == 7
    starts = [s["start"] for s in segments]
    # All distinct.
    assert len(set(round(s, 2) for s in starts)) == 7
    # Monotonic.
    for i in range(1, len(starts)):
        assert starts[i] > starts[i - 1]
    # Matched anchors preserved exactly.
    assert segments[0]["start"] == 10.0
    assert segments[3]["start"] == 15.0
    assert segments[4]["start"] == 30.0
    assert segments[5]["start"] == 40.0
    # New puentes interpolate between 10.0 and 15.0 (10s span / 3 = 1.67 step).
    assert 10.0 < segments[1]["start"] < segments[2]["start"] < 15.0
    # Outro repeat interpolates past last anchor, before audio_dur.
    assert 40.0 < segments[6]["start"] < audio_dur


# ──────────────────────────────────────────────────────────────────────
# All-matched: cleanup didn't add anything new.
# ──────────────────────────────────────────────────────────────────────

def test_all_matched_preserves_synced_times():
    synced_pairs = [
        (5.0, "Línea uno"),
        (10.0, "Línea dos"),
        (15.0, "Línea tres"),
    ]
    cleaned_lines = ["Línea uno", "Línea dos", "Línea tres"]
    audio_dur = 30.0

    segments = align_cleaned_against_synced(cleaned_lines, synced_pairs, audio_dur)

    assert segments is not None
    assert len(segments) == 3
    assert segments[0]["start"] == 5.0
    assert segments[1]["start"] == 10.0
    assert segments[2]["start"] == 15.0


def test_all_matched_with_typo_fixes():
    """Cleanup fixes typos (mía mor → Mi amor); fuzzy match should still
    anchor to the same synced pair."""
    synced_pairs = [
        (5.0, "y vi un corazón que decía: mía mor"),
        (10.0, "llamame al 638"),
    ]
    cleaned_lines = [
        'Y vi un corazón que decía: "Mi amor",',  # typo fixed
        "Llamame al 638",
    ]

    segments = align_cleaned_against_synced(cleaned_lines, synced_pairs, 30.0)
    assert segments is not None
    assert segments[0]["start"] == 5.0
    assert segments[1]["start"] == 10.0


# ──────────────────────────────────────────────────────────────────────
# Repeated chorus: first occurrence anchors, repeats interpolate.
# ──────────────────────────────────────────────────────────────────────

def test_repeated_chorus_anchors_first_occurrence():
    """When cleanup duplicates a chorus line N times but synced only has
    it once, the FIRST cleanup occurrence should anchor to the synced
    time; the rest fill the tail by interpolation."""
    synced_pairs = [
        (10.0, "Verso uno"),
        (20.0, "Llamame al 638"),  # only one synced occurrence
    ]
    cleaned_lines = [
        "Verso uno",
        "Llamame al 638",            # should anchor to 20.0
        "Llamame al 638",            # repeat — interpolate to tail
        "Llamame al 638",            # repeat — interpolate further
        "Llamame al 638",            # repeat — interpolate to end
    ]
    audio_dur = 40.0

    segments = align_cleaned_against_synced(cleaned_lines, synced_pairs, audio_dur)
    assert segments is not None
    assert len(segments) == 5
    assert segments[0]["start"] == 10.0
    assert segments[1]["start"] == 20.0  # first chorus = anchor
    # Repeats spread between 20.0 and audio_dur.
    assert 20.0 < segments[2]["start"] < segments[3]["start"] < segments[4]["start"]
    assert segments[4]["start"] <= audio_dur


# ──────────────────────────────────────────────────────────────────────
# All-new: nothing matches synced; return None for caller fallback.
# ──────────────────────────────────────────────────────────────────────

def test_no_match_returns_none():
    """When zero cleaned lines match any synced pair, return None so the
    caller falls back to the existing FA/whisperX paths."""
    synced_pairs = [
        (5.0, "Algo completamente distinto"),
        (10.0, "Otra cosa que no aparece"),
    ]
    cleaned_lines = [
        "Tenía tantas ganas",
        "Y empecé a pensar",
    ]

    segments = align_cleaned_against_synced(cleaned_lines, synced_pairs, 30.0)
    assert segments is None


# ──────────────────────────────────────────────────────────────────────
# Reordered cleanup: edge case where cleanup put lines out of order.
# ──────────────────────────────────────────────────────────────────────

def test_reordered_cleanup_skips_backwards_anchor():
    """If cleanup somehow reordered lines (very unusual — would happen
    only if Gemini got confused), backwards anchors must be skipped so
    the output stays monotonic."""
    synced_pairs = [
        (5.0, "Primera línea"),
        (10.0, "Segunda línea"),
        (15.0, "Tercera línea"),
    ]
    # Cleanup re-ordered tercera before segunda — backwards anchor.
    cleaned_lines = [
        "Primera línea",
        "Tercera línea",   # would anchor backwards if naive
        "Segunda línea",
    ]
    audio_dur = 30.0

    segments = align_cleaned_against_synced(cleaned_lines, synced_pairs, audio_dur)
    assert segments is not None
    # Strict monotonicity preserved regardless of cleanup's bad order.
    for i in range(1, len(segments)):
        assert segments[i]["start"] > segments[i - 1]["start"]


# ──────────────────────────────────────────────────────────────────────
# Degenerate inputs.
# ──────────────────────────────────────────────────────────────────────

def test_empty_cleaned_returns_none():
    assert align_cleaned_against_synced([], [(5.0, "x")], 30.0) is None


def test_empty_synced_returns_none():
    assert align_cleaned_against_synced(["x"], [], 30.0) is None


def test_zero_audio_dur_returns_none():
    assert align_cleaned_against_synced(["x"], [(5.0, "x")], 0.0) is None


def test_negative_audio_dur_returns_none():
    assert align_cleaned_against_synced(["x"], [(5.0, "x")], -1.0) is None


def test_whitespace_only_lines_filtered():
    """Lines that are only whitespace should be filtered out before alignment."""
    synced_pairs = [(5.0, "real")]
    cleaned_lines = ["", "   ", "real", "\n\t"]
    segments = align_cleaned_against_synced(cleaned_lines, synced_pairs, 30.0)
    assert segments is not None
    assert len(segments) == 1
    assert segments[0]["text"] == "real"


# ──────────────────────────────────────────────────────────────────────
# Output shape validation.
# ──────────────────────────────────────────────────────────────────────

def test_output_segments_have_required_fields():
    synced_pairs = [(5.0, "a"), (10.0, "b")]
    cleaned_lines = ["a", "b"]
    segments = align_cleaned_against_synced(cleaned_lines, synced_pairs, 30.0)
    assert segments is not None
    for s in segments:
        assert set(s.keys()) >= {"start", "end", "text"}
        assert isinstance(s["start"], float)
        assert isinstance(s["end"], float)
        assert isinstance(s["text"], str)
        assert s["end"] > s["start"]


def test_min_seg_duration_enforced():
    """Even when interpolation produces very-close starts, each segment
    has at least MIN_SEG_DUR_S duration so subtitles don't flash."""
    synced_pairs = [(5.0, "primera"), (6.0, "segunda")]
    cleaned_lines = ["primera", "nueva 1", "nueva 2", "nueva 3", "segunda"]
    audio_dur = 30.0
    segments = align_cleaned_against_synced(cleaned_lines, synced_pairs, audio_dur)
    assert segments is not None
    for s in segments[:-1]:
        assert s["end"] - s["start"] >= MIN_SEG_DUR_S - 1e-6


def test_last_segment_ends_at_audio_dur():
    synced_pairs = [(5.0, "primera")]
    cleaned_lines = ["primera", "fin del audio"]
    audio_dur = 30.0
    segments = align_cleaned_against_synced(cleaned_lines, synced_pairs, audio_dur)
    assert segments is not None
    assert segments[-1]["end"] <= audio_dur
    # Allow ≥ MIN_SEG_DUR_S of tail padding.
    assert segments[-1]["end"] >= segments[-1]["start"] + MIN_SEG_DUR_S - 1e-6


# ──────────────────────────────────────────────────────────────────────
# Anti-regression: greedy-monotonic bug.
# ──────────────────────────────────────────────────────────────────────

def test_distant_lexical_match_does_not_starve_later_lines():
    """The earlier greedy-monotonic matcher matched 'llamame al 6380465'
    (cleaned line ~11) to a late synced pair ('llamame al' @02:19) by
    high Jaccard, then starved every subsequent cleaned line of
    anchors. The DP-based matcher must reject this and match
    'Agarré el teléfono' to its real synced pair @01:49 instead."""
    synced_pairs = [
        (10.0, "Verso 1 línea uno"),
        (15.0, "Verso 1 línea dos"),
        (20.0, "y vi un corazón que decía mía mor llamame al"),
        (25.0, "638"),
        (110.0, "Agarré el teléfono y me puse a marcar tenía tantas"),
        (115.0, "ganas de volverte a hablar"),
        (140.0, "llamame al"),
    ]
    cleaned_lines = [
        "Verso 1 línea uno",                          # → 10.0
        "Verso 1 línea dos",                          # → 15.0
        'Y vi un corazón que decía: "Mi amor,',       # → 20.0
        "llamame al 6380465",                         # NEW — should NOT match 140.0
        "Agarré el teléfono y me puse a marcar",      # → 110.0 (the real test)
        "Tenía tantas ganas de volverte a hablar",    # → 115.0
    ]
    audio_dur = 180.0

    segments = align_cleaned_against_synced(cleaned_lines, synced_pairs, audio_dur)
    assert segments is not None
    assert len(segments) == 6
    # Specifically: "Agarré el teléfono" must land near 110.0, not at the tail.
    agarre_seg = next(s for s in segments if "Agarré" in s["text"])
    assert abs(agarre_seg["start"] - 110.0) < 1.0
    # And "Tenía tantas ganas" must land near 115.0.
    tenia_seg = next(s for s in segments if "Tenía tantas ganas" in s["text"])
    assert abs(tenia_seg["start"] - 115.0) < 1.0
