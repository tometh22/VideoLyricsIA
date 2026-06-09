"""Unit tests for ctc_align.py — the Genly CTC timing engine.

Covers the PURE pieces (no torch, no model download, no audio): text
normalization, target building with the synthetic star, span→line
grouping, the collapsed-alignment guard, and the decline-fast paths of
`retime_segments` (flag off / too few lines / missing file), which all
exit BEFORE any heavy import. The acoustic quality itself is covered by
the offline benchmark vs Rotor ground truth (scripts/exp_ctc_align.py),
not by unit tests.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ctc_align  # noqa: E402

# a-z + accented vowels + ñ as a fake vocab; ids arbitrary but unique
VOCAB = {c: i + 5 for i, c in enumerate("abcdefghijklmnopqrstuvwxyzáéíóúñü'")}
STAR = 999


def test_norm_word_keeps_spanish_chars():
    assert ctc_align.norm_word("Canción") == "canción"
    assert ctc_align.norm_word("¡Hola!") == "hola"
    assert ctc_align.norm_word("pingüino") == "pingüino"
    assert ctc_align.norm_word("(woo)") == "woo"
    assert ctc_align.norm_word("123") == ""


def test_build_targets_star_between_lines_only():
    targets, words = ctc_align.build_targets(["la la", "si"], VOCAB, STAR)
    # words: (0,'la',2) (0,'la',2) (-1,'*',1) (1,'si',2) — star NOT after last
    assert [w[0] for w in words] == [0, 0, -1, 1]
    assert targets.count(STAR) == 1
    assert len(targets) == 2 + 2 + 1 + 2


def test_build_targets_skips_unalignable_words():
    targets, words = ctc_align.build_targets(["123 ok"], VOCAB, STAR)
    assert [w[1] for w in words] == ["ok"]


def test_spans_to_lines_groups_words_and_lines():
    # two lines: "ab" / "cd", star between → 5 tokens
    targets, words = ctc_align.build_targets(["ab", "cd"], VOCAB, STAR)
    # frames: ab=[10,20],[20,30]; star=[30,80]; cd=[80,85],[85,90]
    spans = [(10, 20, 0.9), (20, 30, 0.8), (30, 80, 0.5),
             (80, 85, 0.7), (85, 90, 0.6)]
    lines = ctc_align.spans_to_lines(spans, words, 2, frame_to_s=0.02)
    (s0, e0, w0), (s1, e1, w1) = lines
    assert (s0, e0) == (10 * 0.02, 30 * 0.02)
    assert (s1, e1) == (80 * 0.02, 90 * 0.02)
    assert w0[0][0] == "ab" and len(w0) == 1 and len(w1) == 1
    assert s1 >= e0  # monotonic across the star gap


def test_spans_to_lines_none_for_unalignable_line():
    targets, words = ctc_align.build_targets(["ab", "123"], VOCAB, STAR)
    spans = [(10, 20, 0.9), (20, 30, 0.8), (30, 40, 0.5)]
    lines = ctc_align.spans_to_lines(spans, words, 2, frame_to_s=0.02)
    assert lines[0] is not None and lines[1] is None


def test_looks_collapsed_detects_crammed_alignment():
    ok = [(0.0, 2.0, []), (3.0, 5.0, []), (6.0, 8.0, [])]
    assert not ctc_align.looks_collapsed(ok)
    crammed = [(0.0, 0.05, []), (0.1, 0.14, []), (6.0, 8.0, [])]
    assert ctc_align.looks_collapsed(crammed)
    assert ctc_align.looks_collapsed([None, None])


def test_retime_declines_fast_without_torch(monkeypatch, tmp_path):
    segs = [{"text": "hola", "start": 0, "end": 1}] * 5
    # flag off → None before any heavy import
    monkeypatch.delenv("CTC_ALIGN_ENABLED", raising=False)
    assert ctc_align.retime_segments("/nope.wav", segs) is None
    # flag on but too few lines
    monkeypatch.setenv("CTC_ALIGN_ENABLED", "1")
    assert ctc_align.retime_segments("/nope.wav", segs[:2]) is None
    # flag on, enough lines, missing file
    assert ctc_align.retime_segments(str(tmp_path / "missing.wav"), segs) is None


def test_ctc_align_is_valid_timing_source():
    from timing_sources import CTC_ALIGN, VALID_TIMING_SOURCES
    assert CTC_ALIGN in VALID_TIMING_SOURCES
    assert len(CTC_ALIGN) <= 20  # VARCHAR(20) constraint


def test_wrapper_flag_off_is_identity(monkeypatch):
    """THE contract of the PR: with CTC_ALIGN_ENABLED off,
    _maybe_ctc_retime returns the very same object — prod behaviour is
    byte-identical to before the wiring."""
    import asyncio
    from main import _maybe_ctc_retime

    monkeypatch.delenv("CTC_ALIGN_ENABLED", raising=False)
    result = {"job_id": "j1", "segments": [{"text": "hola", "start": 1.0}] * 5}
    out = asyncio.run(_maybe_ctc_retime(result, "/no/such/audio.mp3", "j1"))
    assert out is result  # same object, not a copy


def test_wrapper_never_raises_even_with_flag_on(monkeypatch):
    """Flag ON + missing audio/stem must decline to identity, not 500."""
    import asyncio
    from main import _maybe_ctc_retime

    monkeypatch.setenv("CTC_ALIGN_ENABLED", "1")
    monkeypatch.delenv("VOCAL_SEP_ENABLED", raising=False)  # no stem possible
    result = {"job_id": "j2", "segments": [{"text": "hola", "start": 1.0}] * 5}
    out = asyncio.run(_maybe_ctc_retime(result, "/no/such/audio.mp3", "j2"))
    assert out is result
