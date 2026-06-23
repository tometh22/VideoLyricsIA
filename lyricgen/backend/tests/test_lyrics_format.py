"""Tests for lyrics_format.format_lyrics_pass.

All tests mock the OpenAI client so no network calls are made.
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import lyrics_format as lf


def _seg(text, start=0.0, end=3.0):
    return {"text": text, "start": start, "end": end}


def _result(segs):
    return {"segments": segs, "job_id": "test"}


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ── helpers ──────────────────────────────────────────────────────────────────

def _patch_openai(monkeypatch, lines: list):
    """Patch AsyncOpenAI to return `lines` (without numbering prefix)."""
    numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(lines))

    class _Choice:
        class message:
            content = numbered

    class _Resp:
        choices = [_Choice()]

    class _Completions:
        async def create(self, **_kwargs):
            return _Resp()

    class _Chat:
        completions = _Completions()

    class _FakeClient:
        chat = _Chat()

    monkeypatch.setattr(lf, "_lang_name", lambda _: "Spanish")
    import unittest.mock as mock
    monkeypatch.setattr("lyrics_format.asyncio.wait_for",
                        lambda coro, **_kw: coro)

    import openai
    monkeypatch.setattr(openai, "AsyncOpenAI", lambda: _FakeClient())


# ── tests ─────────────────────────────────────────────────────────────────────

def test_corrects_accents(monkeypatch):
    segs = [_seg("fragil espejo de voz"), _seg("para que tus santos")]
    _patch_openai(monkeypatch, ["Frágil espejo de voz", "¿Para qué tus santos?"])

    result = _run(lf.format_lyrics_pass(_result(segs), language="es"))

    assert result["segments"][0]["text"] == "Frágil espejo de voz"
    assert result["segments"][1]["text"] == "¿Para qué tus santos?"


def test_timestamps_unchanged(monkeypatch):
    segs = [_seg("fragil espejo", 10.0, 14.5)]
    _patch_openai(monkeypatch, ["Frágil espejo"])

    result = _run(lf.format_lyrics_pass(_result(segs), language="es"))

    seg = result["segments"][0]
    assert seg["start"] == 10.0
    assert seg["end"] == 14.5


def test_count_mismatch_returns_original(monkeypatch):
    """If the LLM returns wrong number of lines, original is returned unchanged."""
    segs = [_seg("linea uno"), _seg("linea dos")]
    _patch_openai(monkeypatch, ["Línea uno"])  # only 1 line for 2 segs

    result = _run(lf.format_lyrics_pass(_result(segs), language="es"))

    assert result["segments"][0]["text"] == "linea uno"
    assert result["segments"][1]["text"] == "linea dos"


def test_disabled_by_env(monkeypatch):
    monkeypatch.setenv("LYRICS_FORMAT_ENABLED", "0")
    segs = [_seg("fragil espejo")]
    r = _result(segs)

    result = _run(lf.format_lyrics_pass(r, language="es"))

    assert result is r  # same object, not a copy


def test_empty_segments_returns_original():
    r = {"segments": [], "job_id": "x"}
    result = _run(lf.format_lyrics_pass(r, language="es"))
    assert result is r


def test_non_dict_result_returns_original():
    result = _run(lf.format_lyrics_pass(None, language="es"))  # type: ignore
    assert result is None


def test_openai_failure_returns_original(monkeypatch):
    """Any OpenAI error → original result unchanged."""
    import openai

    class _BrokenClient:
        class chat:
            class completions:
                @staticmethod
                async def create(**_kw):
                    raise RuntimeError("network error")

    monkeypatch.setattr(openai, "AsyncOpenAI", lambda: _BrokenClient())
    segs = [_seg("fragil espejo")]
    r = _result(segs)

    result = _run(lf.format_lyrics_pass(r, language="es"))

    assert result["segments"][0]["text"] == "fragil espejo"
