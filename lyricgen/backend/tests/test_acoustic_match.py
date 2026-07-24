"""Tests for acoustic_match.correct_by_acoustic_similarity.

All tests mock _mfcc_sequence so no audio I/O needed.
_dtw_similarity runs for real (with numpy arrays) — no librosa I/O.

Similarity values:
  identical sequences (np.zeros vs np.zeros) → DTW dist = 0 → sim = 1.0
  very different     (np.zeros vs large-const) → dist >> 0 → sim ≈ 0
"""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import acoustic_match as am


def _seg(text, start, end, word_score=None):
    """Build a minimal segment dict."""
    s = {"text": text, "start": start, "end": end}
    if word_score is not None:
        s["words"] = [{"word": text, "start": start, "end": end, "score": word_score}]
    return s


# ── helpers ──────────────────────────────────────────────────────────────────

def _patch_seq(monkeypatch, mapping: dict):
    """mapping: {(start, end): np.ndarray | None}"""
    def fake(audio_path, start_s, end_s):
        return mapping.get((start_s, end_s))
    monkeypatch.setattr(am, "_mfcc_sequence", fake)


# ── tests ─────────────────────────────────────────────────────────────────────

def test_replaces_low_confidence_with_similar_anchor(monkeypatch, tmp_path):
    """Low-score segment acoustically similar (DTW dist = 0) to anchor → text replaced."""
    audio = str(tmp_path / "audio.wav")
    open(audio, "w").close()

    # Identical MFCC sequences → DTW cost = 0 → similarity = 1.0
    shared_seq = np.zeros((39, 50), dtype=float)

    segments = [
        _seg("Frágil espejo de voz", 110.0, 116.0, word_score=0.85),  # anchor
        _seg("tan miedo, tu don",    127.0, 133.0, word_score=0.40),  # uncertain
    ]
    _patch_seq(monkeypatch, {
        (110.0, 116.0): shared_seq,
        (127.0, 133.0): shared_seq,
    })

    result = am.correct_by_acoustic_similarity(segments, audio, threshold=0.50)

    assert result[0]["text"] == "Frágil espejo de voz"          # anchor unchanged
    assert result[1]["text"] == "Frágil espejo de voz"          # corrected
    assert result[1].get("acoustic_corrected") is True


def test_no_correction_when_texts_already_match(monkeypatch, tmp_path):
    """Same text → no action even if acoustically identical."""
    audio = str(tmp_path / "audio.wav")
    open(audio, "w").close()

    seq = np.zeros((39, 50), dtype=float)
    segments = [
        _seg("Para qué", 50.0, 53.0, word_score=0.90),
        _seg("Para qué", 80.0, 83.0, word_score=0.35),
    ]
    _patch_seq(monkeypatch, {(50.0, 53.0): seq, (80.0, 83.0): seq})

    result = am.correct_by_acoustic_similarity(segments, audio)
    assert result[1]["text"] == "Para qué"
    assert "acoustic_corrected" not in result[1]


def test_no_correction_below_threshold(monkeypatch, tmp_path):
    """Acoustically dissimilar segment (DTW dist >> 0) is left alone."""
    audio = str(tmp_path / "audio.wav")
    open(audio, "w").close()

    anchor_seq = np.zeros((39, 50), dtype=float)
    diff_seq   = np.full((39, 50), 200.0, dtype=float)  # huge distance → sim ≈ 0

    segments = [
        _seg("Frágil espejo de voz", 110.0, 116.0, word_score=0.85),
        _seg("tan miedo, tu don",    127.0, 133.0, word_score=0.40),
    ]
    _patch_seq(monkeypatch, {
        (110.0, 116.0): anchor_seq,
        (127.0, 133.0): diff_seq,
    })

    result = am.correct_by_acoustic_similarity(segments, audio, threshold=0.50)
    assert result[1]["text"] == "tan miedo, tu don"   # unchanged
    assert "acoustic_corrected" not in result[1]


def test_disabled_by_env(monkeypatch, tmp_path):
    """ACOUSTIC_MATCH_ENABLED=0 → returns original list unchanged."""
    monkeypatch.setenv("ACOUSTIC_MATCH_ENABLED", "0")
    audio = str(tmp_path / "audio.wav")
    open(audio, "w").close()

    segments = [
        _seg("Frágil espejo de voz", 110.0, 116.0, word_score=0.85),
        _seg("tan miedo, tu don",    127.0, 133.0, word_score=0.40),
    ]
    result = am.correct_by_acoustic_similarity(segments, audio)
    assert result is segments  # same object returned


def test_skips_adlib_segments(monkeypatch, tmp_path):
    """Adlib (uh/uh) blocks are never used as anchor or target."""
    audio = str(tmp_path / "audio.wav")
    open(audio, "w").close()

    seq = np.zeros((39, 50), dtype=float)
    segments = [
        _seg("uh, uh, uh, uh, uh, uh", 70.0, 95.0, word_score=0.30),  # adlib
        _seg("Frágil espejo de voz",   110.0, 116.0, word_score=0.85),
    ]
    _patch_seq(monkeypatch, {
        (70.0, 95.0):  seq,
        (110.0, 116.0): seq,
    })

    result = am.correct_by_acoustic_similarity(segments, audio)
    assert result[0]["text"] == "uh, uh, uh, uh, uh, uh"  # untouched


def test_missing_audio_returns_original(tmp_path):
    """Non-existent audio path → original list returned without error."""
    segments = [
        _seg("Frágil espejo de voz", 110.0, 116.0, word_score=0.85),
        _seg("tan miedo, tu don",    127.0, 133.0, word_score=0.40),
    ]
    result = am.correct_by_acoustic_similarity(segments, "/no/such/file.wav")
    assert result is segments
