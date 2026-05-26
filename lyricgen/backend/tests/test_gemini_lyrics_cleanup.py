"""Unit tests for `_gemini_cleanup_lyrics` and its content-addressable
cache helpers. Pure data-in/data-out where possible; the Gemini call is
mocked because the live SDK requires Vertex credentials.

Spec recap (see pipeline.py docstring for full context):
- Takes audio_path + lrclib_plain, returns cleaned text or None.
- Gated behind GEMINI_LYRICS_CLEANUP_ENABLED env flag (default off).
- Returns None on missing audio, empty plain, disabled flag, Gemini
  error, suspicious line-count ratio (<50% or >250% of input).
- On success: writes content-addressable cache row (engine="gemini_cleanup").
- INCIDENT 2026-05-26 (Arbol de la vida / Sin Gamulán family): lrclib
  has community-grade defects (missing accents, miscounted chorus
  repeats). This helper closes the gap vs Rotor's licensed LyricFind
  catalog at ~$0.01/song.
"""
import os
import tempfile
import pytest
from unittest.mock import patch, MagicMock

import pipeline


@pytest.fixture
def tiny_audio(tmp_path):
    """A 1KB pseudo-audio file. Content doesn't matter; the file just
    needs to be readable so the hash + Gemini upload paths run."""
    p = tmp_path / "test.wav"
    p.write_bytes(b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 1000)
    return str(p)


# ─── Feature-flag gate ──────────────────────────────────────────────

def test_returns_none_when_flag_off(tiny_audio, monkeypatch):
    """Default behaviour: no Gemini call, no cache touch, returns None."""
    monkeypatch.delenv("GEMINI_LYRICS_CLEANUP_ENABLED", raising=False)
    out = pipeline._gemini_cleanup_lyrics(tiny_audio, "Algun texto")
    assert out is None


def test_returns_none_for_empty_plain(tiny_audio, monkeypatch):
    monkeypatch.setenv("GEMINI_LYRICS_CLEANUP_ENABLED", "1")
    assert pipeline._gemini_cleanup_lyrics(tiny_audio, "") is None
    assert pipeline._gemini_cleanup_lyrics(tiny_audio, "   ") is None
    assert pipeline._gemini_cleanup_lyrics(tiny_audio, None) is None


def test_returns_none_for_missing_audio(monkeypatch):
    monkeypatch.setenv("GEMINI_LYRICS_CLEANUP_ENABLED", "1")
    assert pipeline._gemini_cleanup_lyrics("/no/such/file.wav", "x\ny\nz") is None
    assert pipeline._gemini_cleanup_lyrics("", "x") is None


# ─── Cache key determinism ──────────────────────────────────────────

def test_cache_key_deterministic(tiny_audio):
    """Same audio + same hint → same key. Different hint → different key."""
    k1, ah1, hh1 = pipeline._gemini_cleanup_cache_key(tiny_audio, "hola")
    k2, ah2, hh2 = pipeline._gemini_cleanup_cache_key(tiny_audio, "hola")
    assert k1 == k2
    assert ah1 == ah2
    assert hh1 == hh2

    k3, _, hh3 = pipeline._gemini_cleanup_cache_key(tiny_audio, "adios")
    assert k3 != k1
    assert hh3 != hh1


def test_cache_key_namespace_prefix(tiny_audio):
    """Cache key must be namespaced so it can't collide with whisperX
    cache rows (same DB table, different engine)."""
    key, _, _ = pipeline._gemini_cleanup_cache_key(tiny_audio, "lyrics")
    assert key.startswith("gem-clean:")


def test_cache_key_unreadable_audio_returns_none():
    """Hash failure shouldn't crash — cache is best-effort. Caller still
    proceeds to a live Gemini call without a cache key."""
    k, ah, hh = pipeline._gemini_cleanup_cache_key("/does/not/exist.wav", "x")
    assert k is None
    assert ah is None
    assert hh is None


# ─── Sanity gate on line-count ratio ────────────────────────────────

class _FakeResponse:
    """Mimic google-genai Response with just the attrs we read."""

    def __init__(self, text, finish_reason="STOP"):
        self.text = text
        cand = MagicMock()
        cand.finish_reason = finish_reason
        self.candidates = [cand]


def _patch_genai_with(monkeypatch, fake_response):
    """Helper: replace _get_genai_client + the genai SDK so we don't hit
    Vertex but still exercise the full Gemini-call code path."""
    fake_client = MagicMock()
    fake_client.models.generate_content.return_value = fake_response
    monkeypatch.setattr(pipeline, "_get_genai_client", lambda: fake_client)

    # The function does `from google import genai` at runtime — we need
    # genai.types.Part.from_bytes and genai.types.GenerateContentConfig
    # to exist. The real ones work fine even without credentials (they
    # don't talk to the network), so we don't need to patch them.
    return fake_client


def test_rejects_when_output_too_short(tiny_audio, monkeypatch):
    """If Gemini returns <50% of the input line count, prefer raw text
    over a likely-truncated cleanup. Real incident: Pro hit MAX_TOKENS
    and returned only the first 20 of 58 lines (PR #X exploration)."""
    monkeypatch.setenv("GEMINI_LYRICS_CLEANUP_ENABLED", "1")
    plain = "\n".join(f"line {i}" for i in range(20))   # 20 lines in
    _patch_genai_with(monkeypatch, _FakeResponse("line 1\nline 2\nline 3"))  # 3 out

    out = pipeline._gemini_cleanup_lyrics(tiny_audio, plain)
    assert out is None


def test_rejects_when_output_too_long(tiny_audio, monkeypatch):
    """If Gemini explodes the input to 10x lines, something is wrong
    (probable confusion of song with infinite loop / chorus expansion
    gone wrong). Bail to raw text."""
    monkeypatch.setenv("GEMINI_LYRICS_CLEANUP_ENABLED", "1")
    plain = "\n".join(f"line {i}" for i in range(10))   # 10 lines in
    huge = "\n".join(f"line {i}" for i in range(50))    # 50 lines out (5x)
    _patch_genai_with(monkeypatch, _FakeResponse(huge))

    out = pipeline._gemini_cleanup_lyrics(tiny_audio, plain)
    assert out is None


def test_accepts_modest_expansion(tiny_audio, monkeypatch):
    """A chorus that lrclib counted 3× but the audio actually has 5×
    is a legitimate 1.5x expansion — must pass the sanity gate."""
    monkeypatch.setenv("GEMINI_LYRICS_CLEANUP_ENABLED", "1")
    plain = "\n".join(f"line {i}" for i in range(10))
    cleaned = "\n".join(f"line {i}" for i in range(15))  # 1.5x — within bounds
    _patch_genai_with(monkeypatch, _FakeResponse(cleaned))

    # Disable cache write to avoid touching the DB in tests
    monkeypatch.setattr(pipeline, "_gemini_cleanup_cache_write",
                        lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_gemini_cleanup_cache_lookup",
                        lambda *a, **k: None)

    out = pipeline._gemini_cleanup_lyrics(tiny_audio, plain)
    assert out == cleaned


# ─── Gemini failure modes ───────────────────────────────────────────

def test_gemini_exception_returns_none(tiny_audio, monkeypatch):
    """Any exception in the Gemini call → return None so caller falls
    back to lrclib raw. Never raises to the caller."""
    monkeypatch.setenv("GEMINI_LYRICS_CLEANUP_ENABLED", "1")
    fake_client = MagicMock()
    fake_client.models.generate_content.side_effect = RuntimeError("vertex 500")
    monkeypatch.setattr(pipeline, "_get_genai_client", lambda: fake_client)

    out = pipeline._gemini_cleanup_lyrics(tiny_audio, "a\nb\nc\nd")
    assert out is None


def test_gemini_empty_response_returns_none(tiny_audio, monkeypatch):
    """Safety-filter rejections come back as empty .text — fall back."""
    monkeypatch.setenv("GEMINI_LYRICS_CLEANUP_ENABLED", "1")
    _patch_genai_with(monkeypatch, _FakeResponse("", finish_reason="SAFETY"))

    out = pipeline._gemini_cleanup_lyrics(tiny_audio, "a\nb\nc\nd")
    assert out is None


def test_client_unavailable_returns_none(tiny_audio, monkeypatch):
    """If the Vertex client can't be built (creds missing in some envs),
    return None and let the caller use lrclib raw."""
    monkeypatch.setenv("GEMINI_LYRICS_CLEANUP_ENABLED", "1")

    def _raise():
        raise RuntimeError("creds missing")
    monkeypatch.setattr(pipeline, "_get_genai_client", _raise)

    out = pipeline._gemini_cleanup_lyrics(tiny_audio, "a\nb\nc\nd")
    assert out is None
