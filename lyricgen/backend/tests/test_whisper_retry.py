"""Whisper retry tests (Fase 0 hotfix for the 2026-05-14 503 cascade).

Before the fix, OpenAI Whisper's RateLimitError translated 1-to-1 to HTTP 503
with no retry — Agus + admin transcribiendo en simultáneo gatillaron el
rate-limit y ambos vieron el error en sus pantallas.

After the fix, _transcribe_via_openai_api retries up to WHISPER_MAX_RETRIES
(default 5) with exponential backoff + jitter before surrendering. Only if
ALL retries fail, surface 503 — and now with Retry-After:60 since the
backoff already ate the short transients.
"""

import io
from unittest.mock import MagicMock

import pytest


def _make_rate_limit_exc():
    """Build a RateLimitError matching the SDK's isinstance check."""
    from openai import RateLimitError
    err = RateLimitError.__new__(RateLimitError)
    err.message = "Rate limit reached"
    err.args = ("Rate limit reached",)
    return err


def _make_api_error_exc():
    from openai import APIError
    err = APIError.__new__(APIError)
    err.message = "Internal server error"
    err.args = ("Internal server error",)
    return err


def _stub_whisper_response(text="hola mundo"):
    """Fake successful Whisper API response. The real SDK returns segments
    as objects with `.text`, `.start`, `.end` attributes (not dicts), so
    we mirror that shape — the post-call code does `seg.text`, not `seg["text"]`."""
    seg = MagicMock()
    seg.text = text
    seg.start = 0.0
    seg.end = 1.0
    seg.no_speech_prob = 0.0  # high-confidence speech; not filtered
    seg.avg_logprob = -0.1
    resp = MagicMock()
    resp.text = text
    resp.segments = [seg]
    resp.language = "es"
    return resp


@pytest.fixture
def patched_transcribe(monkeypatch):
    """Returns a factory `setup(side_effects)` that wires the retry loop's
    dependencies to controlled mocks. Returns the create_mock so tests can
    assert call_count."""
    import pipeline

    def setup(side_effects):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-xxx")
        # Don't delenv WHISPER_MAX_RETRIES — tests that explicitly set it
        # before calling setup() need their value preserved.
        if "WHISPER_MAX_RETRIES" not in __import__("os").environ:
            pass  # default 5 applies
        # Make sleeps instant — the test should NOT actually wait 30s.
        monkeypatch.setattr("time.sleep", lambda s: None)
        monkeypatch.setattr("random.uniform", lambda a, b: 0.0)

        # Patch the OpenAI client construction. The function does
        # `from openai import OpenAI; client = OpenAI()` inline, so we
        # patch the source class — the inline import resolves to it.
        import openai
        fake_client = MagicMock()
        create_mock = MagicMock(side_effect=side_effects)
        fake_client.audio.transcriptions.create = create_mock
        monkeypatch.setattr(openai, "OpenAI", lambda *a, **kw: fake_client)

        # Skip ffmpeg compression — return the path unchanged.
        monkeypatch.setattr(pipeline, "_compress_for_whisper", lambda p: p)
        # Skip provenance recording (the function imports it inline from
        # the provenance module; patch at the source).
        import provenance
        monkeypatch.setattr(provenance, "record_ai_call", lambda **kw: None)

        # `open(api_path, "rb")` must succeed without a real file on disk.
        real_open = open
        def fake_open(path, mode="r", *a, **kw):
            if "b" in mode and isinstance(path, str) and path.endswith(".mp3"):
                return io.BytesIO(b"\x00" * 1024)
            return real_open(path, mode, *a, **kw)
        monkeypatch.setattr("builtins.open", fake_open)

        return create_mock

    return setup


def test_succeeds_on_first_attempt(patched_transcribe):
    """Happy path: Whisper returns OK first try → 1 API call only."""
    import pipeline
    create_mock = patched_transcribe([_stub_whisper_response()])
    result = pipeline._transcribe_via_openai_api("/tmp/_test.mp3", language="es")
    assert create_mock.call_count == 1
    assert result is not None


def test_retries_then_succeeds_on_3rd(patched_transcribe):
    """Rate-limit on 1st and 2nd attempts → 3rd succeeds. User never sees error."""
    import pipeline
    rate = _make_rate_limit_exc()
    create_mock = patched_transcribe([rate, rate, _stub_whisper_response()])
    result = pipeline._transcribe_via_openai_api("/tmp/_test.mp3", language="es")
    assert create_mock.call_count == 3
    assert result is not None


def test_exhausts_retries_then_503(patched_transcribe):
    """5 consecutive rate-limits → 503 with Retry-After:60 (NOT the old
    instant 503 that just kicked the problem to the user)."""
    import pipeline
    from fastapi import HTTPException
    rate = _make_rate_limit_exc()
    create_mock = patched_transcribe([rate] * 5)

    with pytest.raises(HTTPException) as exc_info:
        pipeline._transcribe_via_openai_api("/tmp/_test.mp3", language="es")

    assert exc_info.value.status_code == 503
    assert "saturado tras 5 reintentos" in exc_info.value.detail
    assert exc_info.value.headers.get("Retry-After") == "60"
    assert create_mock.call_count == 5


def test_api_error_not_retryable(patched_transcribe):
    """APIError (e.g. 500 from OpenAI, malformed audio) is NOT transient.
    Retrying just amplifies the failure — bail immediately as 502."""
    import pipeline
    from fastapi import HTTPException
    api = _make_api_error_exc()
    create_mock = patched_transcribe([api])

    with pytest.raises(HTTPException) as exc_info:
        pipeline._transcribe_via_openai_api("/tmp/_test.mp3", language="es")

    assert exc_info.value.status_code == 502
    # Critical: only 1 attempt — no retry storm on persistent errors.
    assert create_mock.call_count == 1


def test_env_var_caps_retries(patched_transcribe, monkeypatch):
    """WHISPER_MAX_RETRIES=2 → bail after 2 attempts. Lets ops tune the
    budget on Railway without redeploying code."""
    import pipeline
    from fastapi import HTTPException
    monkeypatch.setenv("WHISPER_MAX_RETRIES", "2")
    rate = _make_rate_limit_exc()
    create_mock = patched_transcribe([rate, rate])

    with pytest.raises(HTTPException):
        pipeline._transcribe_via_openai_api("/tmp/_test.mp3", language="es")

    assert create_mock.call_count == 2
