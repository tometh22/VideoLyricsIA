"""Tests for acoustic_match.correct_by_acoustic_similarity.

All tests mock _mel_embedding so no audio I/O or librosa dependency.
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

def _patch_embed(monkeypatch, mapping: dict):
    """mapping: {(start, end): np.ndarray | None}"""
    def fake(audio_path, start_s, end_s):
        return mapping.get((start_s, end_s))
    monkeypatch.setattr(am, "_mel_embedding", fake)


# ── tests ─────────────────────────────────────────────────────────────────────

def test_replaces_low_confidence_with_similar_anchor(monkeypatch, tmp_path):
    """Low-score segment acoustically similar to high-score anchor → text replaced."""
    audio = str(tmp_path / "audio.wav")
    open(audio, "w").close()            # dummy file so os.path.exists passes

    anchor_emb = np.array([1.0, 0.0] * 32, dtype=float)  # (64,)
    uncert_emb = np.array([0.99, 0.0] * 32, dtype=float) # very similar

    segments = [
        _seg("Frágil espejo de voz", 110.0, 116.0, word_score=0.85),  # anchor
        _seg("tan miedo, tu don",    127.0, 133.0, word_score=0.40),  # uncertain
    ]
    _patch_embed(monkeypatch, {
        (110.0, 116.0): anchor_emb,
        (127.0, 133.0): uncert_emb,
    })

    result = am.correct_by_acoustic_similarity(segments, audio, threshold=0.80)

    assert result[0]["text"] == "Frágil espejo de voz"          # anchor unchanged
    assert result[1]["text"] == "Frágil espejo de voz"          # corrected
    assert result[1].get("acoustic_corrected") is True


def test_no_correction_when_texts_already_match(monkeypatch, tmp_path):
    """Same text → no action even if acoustically similar."""
    audio = str(tmp_path / "audio.wav")
    open(audio, "w").close()

    emb = np.ones(64)
    segments = [
        _seg("Para qué", 50.0, 53.0, word_score=0.90),
        _seg("Para qué", 80.0, 83.0, word_score=0.35),
    ]
    _patch_embed(monkeypatch, {(50.0, 53.0): emb, (80.0, 83.0): emb})

    result = am.correct_by_acoustic_similarity(segments, audio)
    assert result[1]["text"] == "Para qué"
    assert "acoustic_corrected" not in result[1]


def test_no_correction_below_threshold(monkeypatch, tmp_path):
    """Acoustically dissimilar segment is left alone."""
    audio = str(tmp_path / "audio.wav")
    open(audio, "w").close()

    anchor_emb = np.array([1.0, 0.0] * 32)
    diff_emb   = np.array([0.0, 1.0] * 32)  # orthogonal → cosine = 0

    segments = [
        _seg("Frágil espejo de voz", 110.0, 116.0, word_score=0.85),
        _seg("tan miedo, tu don",    127.0, 133.0, word_score=0.40),
    ]
    _patch_embed(monkeypatch, {
        (110.0, 116.0): anchor_emb,
        (127.0, 133.0): diff_emb,
    })

    result = am.correct_by_acoustic_similarity(segments, audio, threshold=0.80)
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

    emb = np.ones(64)
    segments = [
        _seg("uh, uh, uh, uh, uh, uh", 70.0, 95.0, word_score=0.30),  # adlib
        _seg("Frágil espejo de voz",   110.0, 116.0, word_score=0.85),
    ]
    # If adlib were used as uncertain target, it might get "Frágil espejo" text
    _patch_embed(monkeypatch, {
        (70.0, 95.0):  emb,
        (110.0, 116.0): emb,
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
