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
    # Desde el fix del presupuesto (incidente 2026-08-26/28) `call_with_budget`
    # va SIEMPRE por `predictions.create + poll` — el único camino que corta en
    # el deadline. `replicate.run` ya no se usa con modelos pinneados. Un fake
    # inyectado por otro módulo de tests puede no tenerlo: lo aseguramos acá.
    if not hasattr(sys.modules["replicate"], "predictions"):
        sys.modules["replicate"].predictions = types.SimpleNamespace(
            create=MagicMock())
    import whisperx_transcribe
    importlib.reload(whisperx_transcribe)        # pick up our env + new code
    return whisperx_transcribe


@pytest.fixture
def fake_audio():
    """A real temp file so the `os.path.exists` guard passes.

    NOTE: writes RANDOM bytes (not a fixed RIFF stub) so every call
    produces a different content hash. PR-3 (whisperX cache 2026-05-25)
    keys by audio content sha256 — without unique bytes, two tests
    sharing the same fake audio would short-circuit on cache hit and
    `replicate.run` mock would never be invoked, breaking call_args
    assertions."""
    f = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    f.write(b"RIFF$\x00\x00\x00WAVEfmt " + os.urandom(64))
    f.close()
    yield f.name
    try:
        os.unlink(f.name)
    except OSError:
        pass


def _mock_replicate_run():
    """Mock de `replicate.predictions.create` que devuelve una prediction ya
    `succeeded` con un resultado whisperX mínimo pero válido.

    Sigue exponiendo el `input` capturado en `m.call_args` para que los tests
    inspeccionen `initial_prompt`: `predictions.create(version=..., input=...)`
    lo pasa como kwarg igual que lo hacía `replicate.run(model, input=...)`.
    """
    fake_output = {
        "segments": [
            {"start": 0.0, "end": 1.0, "text": "Legalícenla",
             "words": [{"word": "Legalícenla", "start": 0.0, "end": 1.0, "score": 0.95}]},
            {"start": 1.5, "end": 2.5, "text": "Legalícenla",
             "words": [{"word": "Legalícenla", "start": 1.5, "end": 2.5, "score": 0.94}]},
        ]
    }
    class _Succeeded:
        status = "succeeded"
        logs = ""
        error = None
        output = fake_output

        def reload(self):
            pass

        def cancel(self):
            pass

    return MagicMock(return_value=_Succeeded())


def test_initial_prompt_omitted_when_no_hint(fake_audio):
    wx = _force_enabled()
    with patch("replicate.predictions.create", _mock_replicate_run()) as m:
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
    with patch("replicate.predictions.create", _mock_replicate_run()) as m:
        result = wx.transcribe_whisperx(fake_audio, language="es", lyrics_hint=lyrics)
    assert result is not None
    _model, kwargs = m.call_args
    assert "initial_prompt" in kwargs["input"]
    sent = kwargs["input"]["initial_prompt"]
    assert "Legalícenla" in sent
    # Cap revised 2026-05-25 after live A/B: 120 chars >> 800. Long
    # prompts saturate faster-whisper attention.
    assert len(sent) <= 120


def test_initial_prompt_capped_at_120_chars(fake_audio):
    """EMPIRICAL (2026-05-25 live test against Replicate whisperX): a long
    prompt (full lyrics, 800 chars) HURTS — output collapses to 2
    distorted segments because the model parrots the prompt vocabulary
    across all audio. The sweet spot is 2-4 seed lines, capped at 120
    chars total. Sending more saturates attention."""
    wx = _force_enabled()
    lyrics = ("Una linea larga que excede facilmente cien caracteres por si sola "
              "para forzar el truncado. " * 5)
    with patch("replicate.predictions.create", _mock_replicate_run()) as m:
        wx.transcribe_whisperx(fake_audio, language="es", lyrics_hint=lyrics)
    _, kwargs = m.call_args
    sent = kwargs["input"]["initial_prompt"]
    assert len(sent) <= 120, f"expected ≤120 chars, got {len(sent)}"


def test_initial_prompt_extracts_first_seed_lines(fake_audio):
    """Verify the seed extraction: takes the first 2-4 non-empty lines
    (typically chorus / opening — densest in distinctive words), joined
    with '. ' for whisperX's chunked attention."""
    wx = _force_enabled()
    lyrics = (
        "Legalícenla\n"
        "Legalícenla\n"
        "Oh-oh-oh\n"
        "Hubo tiempos de guerras, tiempos de paz\n"
        "Hubo un tiempo en que era ilegal"
    )
    with patch("replicate.predictions.create", _mock_replicate_run()) as m:
        wx.transcribe_whisperx(fake_audio, language="es", lyrics_hint=lyrics)
    _, kwargs = m.call_args
    sent = kwargs["input"]["initial_prompt"]
    assert "Legalícenla" in sent
    assert sent.startswith("Legalícenla")
    assert ". " in sent                       # joins with ". "
    assert len(sent) <= 120


def test_initial_prompt_skipped_when_empty_string(tmp_path):
    """Empty / whitespace-only hint should NOT leak an empty initial_prompt
    into the payload (would tell whisperX 'expect nothing', which biases
    AGAINST any reasonable transcription).

    NOTE: needs two DISTINCT audio files because PR-3 (whisperX cache
    2026-05-25) keys by content sha256 + lang + hint_hash. Both calls
    here have the same hint_hash (empty string after .strip()), so
    reusing the same audio would cache-hit the 2nd call and break the
    mock assertion.
    """
    wx = _force_enabled()
    audio_a = tmp_path / "a.wav"
    audio_a.write_bytes(b"RIFF$\x00\x00\x00WAVEfmt " + os.urandom(64))
    audio_b = tmp_path / "b.wav"
    audio_b.write_bytes(b"RIFF$\x00\x00\x00WAVEfmt " + os.urandom(64))

    with patch("replicate.predictions.create", _mock_replicate_run()) as m:
        wx.transcribe_whisperx(str(audio_a), language="es", lyrics_hint="")
    _, kwargs = m.call_args
    assert "initial_prompt" not in kwargs["input"]

    with patch("replicate.predictions.create", _mock_replicate_run()) as m:
        wx.transcribe_whisperx(str(audio_b), language="es", lyrics_hint="   \n  ")
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
