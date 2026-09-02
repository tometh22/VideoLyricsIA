"""Regression coverage for mixed-language transcription (job 1a8d7b9fef07)."""

import ast
import unicodedata
from pathlib import Path

import pytest

from transcription_language import (
    detect_text_language,
    detect_text_languages,
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

LOS_PERICOS_REFERENCE = """
Hoy temprano estuve pensando en vos
Paso el tiempo y ahora me siento mejor
Cuando puedo, no puedo, no puedo, no puedo
Ya no hay nada ni nadie que te quiera atar
Oh no, no, no, oh no, no te hice daño
Si estás lejos de mí
Oh no, no, no, no, no, oh no, no te hice daño
Y te alejaste de mí
Real, wow wow
Real, wow wow
Real, wow wow
Real, wow wow
¡no!
¡nooooooooooooooooooooooooooooooooooooooooooooooo!
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


def test_bilingual_reference_stays_multi_label():
    reference = (
        "Yo tengo una razón para cambiar la noche y nunca volver atrás.\n"
        "The night is falling and I can hear you tonight because you will leave me alone."
    )
    assert detect_text_languages(reference) == {"es", "en"}
    assert detect_text_language(reference) is None
    assert resolve_transcription_language("", reference_text=reference) is None


def test_repeated_common_token_does_not_misclassify_los_pericos_as_portuguese():
    assert detect_text_language(LOS_PERICOS_REFERENCE) == "es"


def test_repeating_one_marker_cannot_manufacture_language_confidence():
    assert detect_text_language("no " * 100) is None


def test_spanish_common_words_cannot_force_portuguese():
    text = "Nos vemos, no sé por qué somos así, porque se terminó"
    assert detect_text_language(text) in ("es", None)


def test_unaccented_spanish_cannot_force_french():
    text = "Tu que no sabes de la vida, un amor en mi corazon"
    assert detect_text_language(text) in ("es", None)


def test_neighboring_unsupported_romance_languages_stay_auto():
    catalan = "La nit de sempre, una llum que no mor en el temps"
    galician = "Eu non sei por que somos así, sempre na noite de onte"
    assert detect_text_language(catalan) is None
    assert detect_text_language(galician) is None


def test_unicode_nfc_and_nfd_resolve_identically():
    text = "Hoy estás aquí, quizás mañana estarás más lejos"
    assert detect_text_language(text) == detect_text_language(
        unicodedata.normalize("NFD", text)
    )


def test_compact_mixed_language_reference_stays_auto():
    text = "You and me, we are with; hoy siempre estoy aquí"
    assert detect_text_language(text) is None


@pytest.mark.parametrize(
    ("language", "text"),
    [
        ("en", "You and me " * 12),
        ("pt", "Eu não quero " * 12),
        ("fr", "Je ne veux pas " * 12),
        ("de", "Ich und du " * 12),
    ],
)
def test_repetitive_but_diagnostic_choruses_still_resolve(language, text):
    assert detect_text_language(text) == language


def test_common_contractions_still_expose_language_evidence():
    english = "I'm with you, don't leave me, we're alone tonight"
    french = "J'ai peur, c'est l'amour, qu'il ne m'abandonne pas"
    assert detect_text_language(english) == "en"
    assert detect_text_language(french) == "fr"


@pytest.mark.parametrize(
    ("language", "text"),
    [
        ("en", "The light of day is with you and we have all been here"),
        ("pt", "Eu não quero você longe de nós porque hoje estou muito bem"),
        ("fr", "Je suis avec vous dans cette maison mais elle est très loin"),
        ("it", "Io sono con lei in questa casa perché noi siamo molto vicini"),
        ("de", "Ich bin mit dir und wir sind auch hier weil das sehr schön ist"),
    ],
)
def test_supported_language_detection_is_preserved(language, text):
    assert detect_text_language(text) == language


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
