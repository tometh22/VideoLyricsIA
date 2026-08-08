"""Regression coverage for mixed-language transcription (job 1a8d7b9fef07)."""

import ast
from pathlib import Path

from transcription_language import (
    detect_text_language,
    normalize_language,
    resolve_transcription_language,
)


ENGLISH_REFERENCE = """
Through the course of an embrace
Our sisters felt a striking hand
Their fear was raised by the light of day
We have a reason to change our mind
Like me so much, don't think I will see them soon
"""

SPANISH_REFERENCE = """
Yo tengo una razón para cambiar
La noche se queda con nosotros
Cuando estoy contigo no quiero mirar atrás
Siempre vuelve la canción que somos
"""


def test_auto_detects_english_reference_before_postprocessing():
    assert resolve_transcription_language(
        "",
        reference_text=ENGLISH_REFERENCE,
    ) == "en"


def test_auto_falls_back_to_recognized_segments():
    result = {
        "segments": [
            {"text": "We have a reason to change our mind"},
            {"text": "The light of day is with you and me"},
        ]
    }
    assert resolve_transcription_language("", result=result) == "en"


def test_explicit_operator_choice_wins_over_reference_detection():
    assert resolve_transcription_language(
        "es",
        reference_text=ENGLISH_REFERENCE,
    ) == "es"


def test_spanish_auto_detection_remains_supported():
    assert detect_text_language(SPANISH_REFERENCE) == "es"


def test_short_or_ambiguous_text_stays_provider_auto():
    assert detect_text_language("Oh oh sister midnight") is None
    assert resolve_transcription_language("", result={"segments": []}) is None


def test_normalize_accepts_supported_locale_and_rejects_unknown_values():
    assert normalize_language("EN-us") == "en"
    assert normalize_language(" pt_BR ") == "pt"
    assert normalize_language("jv") is None


def test_adlib_window_transcriber_forwards_english(monkeypatch, tmp_path):
    """The secondary ASR witness must not keep the old hard-coded ``es``."""
    import main
    import pipeline
    import subprocess

    stem = tmp_path / "vocals.wav"
    stem.write_bytes(b"stem")
    captured = {}

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: None)

    def fake_transcribe(path, *, language=None, **kwargs):
        captured["language"] = language
        return [{"text": "Sister midnight"}]

    monkeypatch.setattr(pipeline, "_transcribe_via_openai_api", fake_transcribe)

    transcribe_window = main._make_stem_window_transcriber(
        str(stem),
        language="en",
    )
    assert transcribe_window(10.0, 12.0) == "Sister midnight"
    assert captured["language"] == "en"


def test_auto_language_is_resolved_before_primary_whisperx():
    """Reference language must guide the first ASR, not only post-passes."""
    tree = ast.parse((Path(__file__).parents[1] / "main.py").read_text())
    runner = next(
        node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_run_transcription_for_job"
    )
    calls = [node for node in ast.walk(runner) if isinstance(node, ast.Call)]
    resolution_lines = [
        node.lineno for node in calls
        if getattr(node.func, "id", None) == "resolve_transcription_language"
    ]
    whisperx_lines = [
        node.lineno for node in ast.walk(runner)
        if isinstance(node, ast.Attribute)
        and node.attr == "transcribe_whisperx"
    ]
    assert resolution_lines and whisperx_lines
    assert min(resolution_lines) < min(whisperx_lines)
