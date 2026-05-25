"""Unit tests for the transcription post-processing helpers.

Covers:
- `normalize_words` (PR-D / Bug B contract — score-aware word stripping)
- `dedup_collisions` (2026-05-25 fix — merge duplicate emit collisions
  forced_align produces when the lyrics text has repeated chorus lines)

The functions are pure, side-effect-free, and don't touch Replicate / DB.
Test them as data → data transforms without fixtures.
"""
from __future__ import annotations
import pytest

from transcribe_postprocess import normalize_words, dedup_collisions


# ─── normalize_words (existing contract) ───────────────────────────


def test_normalize_passes_through_segments_without_words():
    segs = [{"start": 0, "end": 1, "text": "hi"}]
    assert normalize_words(segs) == segs


def test_normalize_keeps_words_when_score_present_fa_path():
    segs = [{
        "start": 0, "end": 1, "text": "hi",
        "words": [{"word": "hi", "start": 0, "end": 1, "score": 0.95}],
    }]
    out = normalize_words(segs)
    assert "words" in out[0]
    assert out[0]["words"][0]["score"] == 0.95


def test_normalize_strips_words_when_no_score_whisper_raw_path():
    segs = [{
        "start": 0, "end": 1, "text": "hi",
        "words": [{"word": "hi", "start": 0, "end": 1}],
    }]
    out = normalize_words(segs)
    assert "words" not in out[0]


# ─── dedup_collisions (2026-05-25 fix) ────────────────────────────


def test_dedup_passes_empty():
    assert dedup_collisions([]) == []


def test_dedup_passes_single_segment_unchanged():
    segs = [{"start": 0.0, "end": 1.0, "text": "a"}]
    assert dedup_collisions(segs) == [{"start": 0.0, "end": 1.0, "text": "a"}]


def test_dedup_merges_identical_text_within_epsilon():
    """The canonical case from the 2026-05-25 incident: forced_align emitted
    two "Legalícenla" at 0:45.7 with the SAME timestamp (the operator saw
    them in the list view at "0:45.7 / 0:45.7") — they collapse to one with
    the widest end window."""
    segs = [
        {"start": 45.7, "end": 46.5, "text": "Legalícenla"},
        {"start": 45.7, "end": 47.0, "text": "Legalícenla"},
    ]
    out = dedup_collisions(segs)
    assert len(out) == 1
    assert out[0]["text"] == "Legalícenla"
    assert out[0]["start"] == 45.7
    assert out[0]["end"] == 47.0


def test_dedup_merges_when_starts_are_close_but_not_equal():
    """50 ms apart is functionally simultaneous from the bake's POV (the
    operator can't see the difference). The epsilon (100 ms) catches these."""
    segs = [
        {"start": 45.7, "end": 46.5, "text": "Legalícenla"},
        {"start": 45.75, "end": 46.7, "text": "Legalícenla"},
    ]
    out = dedup_collisions(segs)
    assert len(out) == 1
    assert out[0]["end"] == 46.7


def test_dedup_does_not_merge_different_text_at_same_start():
    """Chorus harmony: "Legalícenla" and "Oh-oh-oh" sung simultaneously is
    legitimate overlap, not a dedup case. Frontend stacks them; backend keeps
    both."""
    segs = [
        {"start": 49.3, "end": 50.0, "text": "Legalícenla"},
        {"start": 49.3, "end": 50.0, "text": "Oh-oh-oh"},
    ]
    out = dedup_collisions(segs)
    assert len(out) == 2


def test_dedup_does_not_merge_same_text_far_apart():
    """A chorus that repeats "Legalícenla" three times across the song must
    keep all three — they sit at distinct timestamps, are NOT a dedup case."""
    segs = [
        {"start": 45.4, "end": 46.5, "text": "Legalícenla"},
        {"start": 90.0, "end": 91.2, "text": "Legalícenla"},
        {"start": 180.0, "end": 181.3, "text": "Legalícenla"},
    ]
    out = dedup_collisions(segs)
    assert len(out) == 3


def test_dedup_is_case_and_whitespace_insensitive():
    """forced_align casing/whitespace can vary; treat as the same line."""
    segs = [
        {"start": 1.0, "end": 2.0, "text": "Legalícenla"},
        {"start": 1.05, "end": 2.1, "text": "  legalícenla "},
    ]
    out = dedup_collisions(segs)
    assert len(out) == 1


def test_dedup_handles_unsorted_input():
    """The aligner sometimes emits out-of-order — sort + merge."""
    segs = [
        {"start": 2.0, "end": 3.0, "text": "b"},
        {"start": 1.0, "end": 1.5, "text": "a"},
        {"start": 1.05, "end": 1.6, "text": "a"},
    ]
    out = dedup_collisions(segs)
    assert len(out) == 2
    assert out[0]["text"] == "a"
    assert out[0]["start"] == 1.0
    assert out[0]["end"] == 1.6
    assert out[1]["text"] == "b"


def test_dedup_does_not_mutate_input():
    segs = [
        {"start": 1.0, "end": 2.0, "text": "x"},
        {"start": 1.05, "end": 2.1, "text": "x"},
    ]
    before = [dict(s) for s in segs]
    _ = dedup_collisions(segs)
    assert segs == before


def test_dedup_respects_custom_epsilon():
    """The epsilon is configurable; the default 100 ms catches the common
    case. With a tiny epsilon (1 ms), two segments 50 ms apart should NOT
    merge."""
    segs = [
        {"start": 1.0, "end": 1.5, "text": "x"},
        {"start": 1.05, "end": 1.55, "text": "x"},
    ]
    out = dedup_collisions(segs, epsilon_s=0.001)
    assert len(out) == 2
