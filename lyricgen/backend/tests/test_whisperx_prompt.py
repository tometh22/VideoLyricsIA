"""Verify lyrics-aware prompt is forwarded to Replicate whisperX.

We can't call Replicate in unit tests (network + cost). Instead we mock
`replicate.run` and capture the `input` dict it received. The contract:
when `transcribe_whisperx` is called with `lyrics_hint`, the input must
contain `initial_prompt` populated with the (first ~800 chars of the)
lyrics text.

INCIDENT 2026-05-25: PROD without this hint produced ["Le realizan la"]
× 4 in the intro of Legalícenla because the audio is phonemically
ambiguous on a heavy electric-guitar mix. The fix passes lrclib/Genius
text as initial_prompt so whisperX/faster-whisper biases its vocab.
"""
from __future__ import annotations
import importlib
import os
import sys
import tempfile
import types
from unittest.mock import patch, MagicMock

import pytest


def _force_enabled():
    """Activate the module by setting both env vars expected by `is_enabled`
    AND injecting a fake `replicate` module (the SDK isn't a test dep)."""
    os.environ["WHISPERX_ENABLED"] = "1"
    os.environ["REPLICATE_API_TOKEN"] = "test-token-stub"
    # Inject a fake `replicate` module BEFORE importing whisperx_transcribe
    # so the `import replicate` inside the function picks up our stub. The
    # stub's `run` is a no-op MagicMock; tests replace it as needed.
    if "replicate" not in sys.modules:
        fake = types.ModuleType("replicate")
        fake.run = MagicMock()
        sys.modules["replicate"] = fake
    import whisperx_transcribe
    importlib.reload(whisperx_transcribe)        # pick up our env + new code
    return whisperx_transcribe


@pytest.fixture
def fake_audio():
    """A real temp file so the `os.path.exists` guard passes."""
    f = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    f.write(b"RIFF$\x00\x00\x00WAVEfmt ")     # minimal header, not real audio
    f.close()
    yield f.name
    try:
        os.unlink(f.name)
    except OSError:
        pass


def _mock_replicate_run():
    """Return a mock that replays a tiny but valid whisperX result so the
    function returns segments and we can assert. The captured `input` arg
    is exposed on the mock for the test to inspect."""
    fake_output = {
        "segments": [
            {"start": 0.0, "end": 1.0, "text": "Legalícenla",
             "words": [{"word": "Legalícenla", "start": 0.0, "end": 1.0, "score": 0.95}]},
            {"start": 1.5, "end": 2.5, "text": "Legalícenla",
             "words": [{"word": "Legalícenla", "start": 1.5, "end": 2.5, "score": 0.94}]},
        ]
    }
    return MagicMock(return_value=fake_output)


def test_initial_prompt_omitted_when_no_hint(fake_audio):
    wx = _force_enabled()
    with patch("replicate.run", _mock_replicate_run()) as m:
        result = wx.transcribe_whisperx(fake_audio, language="es")
    assert result is not None
    # call_args = ((model_str,), {"input": {...}})
    _model, kwargs = m.call_args
    assert "input" in kwargs
    assert "initial_prompt" not in kwargs["input"], \
        "no hint passed → initial_prompt must NOT be in payload"


def test_initial_prompt_included_when_hint_passed(fake_audio):
    wx = _force_enabled()
    lyrics = (
        "Legalícenla\nLegalícenla\nLegalícenla\nOh-oh-oh\n"
        "Legalícenla\nOh-oh-oh\nHubo tiempos de guerras"
    )
    with patch("replicate.run", _mock_replicate_run()) as m:
        result = wx.transcribe_whisperx(fake_audio, language="es", lyrics_hint=lyrics)
    assert result is not None
    _model, kwargs = m.call_args
    assert "initial_prompt" in kwargs["input"]
    sent = kwargs["input"]["initial_prompt"]
    assert "Legalícenla" in sent
    assert "Oh-oh-oh" in sent
    # Length cap: at most 800 chars (~200 tokens for Spanish/English)
    assert len(sent) <= 800


def test_initial_prompt_truncated_for_long_lyrics(fake_audio):
    """Whisper itself reads only the last ~224 tokens of the prompt.
    Sending more is wasted bytes — and on a 10k-char Gemini result we'd
    blow the Replicate input size unnecessarily. Cap is 800 chars."""
    wx = _force_enabled()
    lyrics = "Legalícenla " * 200          # 2400+ chars
    with patch("replicate.run", _mock_replicate_run()) as m:
        wx.transcribe_whisperx(fake_audio, language="es", lyrics_hint=lyrics)
    _, kwargs = m.call_args
    sent = kwargs["input"]["initial_prompt"]
    assert len(sent) == 800, f"expected exactly 800 chars, got {len(sent)}"


def test_initial_prompt_skipped_when_empty_string(fake_audio):
    """Empty / whitespace-only hint should NOT leak an empty initial_prompt
    into the payload (would tell whisperX 'expect nothing', which biases
    AGAINST any reasonable transcription)."""
    wx = _force_enabled()
    with patch("replicate.run", _mock_replicate_run()) as m:
        wx.transcribe_whisperx(fake_audio, language="es", lyrics_hint="")
    _, kwargs = m.call_args
    assert "initial_prompt" not in kwargs["input"]

    with patch("replicate.run", _mock_replicate_run()) as m:
        wx.transcribe_whisperx(fake_audio, language="es", lyrics_hint="   \n  ")
    _, kwargs = m.call_args
    assert "initial_prompt" not in kwargs["input"]


def test_signature_has_lyrics_hint_param():
    """AST regression: ensure no one accidentally removes the param."""
    wx = _force_enabled()
    import inspect
    params = inspect.signature(wx.transcribe_whisperx).parameters
    assert "lyrics_hint" in params, "lyrics_hint param missing — was it removed?"
    # Must have a None default to keep the old call-sites working.
    assert params["lyrics_hint"].default is None
