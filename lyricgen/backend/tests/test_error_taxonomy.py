"""Tests del clasificador de errores (error_taxonomy.classify_error).

Table-driven con mensajes REALES de los raise sites de pipeline.py — si
alguien cambia un mensaje de error o el orden de las reglas, estos tests
fijan el comportamiento esperado del dashboard de actividad.
"""
import pytest

from error_taxonomy import CATEGORIES, UNKNOWN, classify_error, public_error


# (mensaje real, categoría esperada)
CASES = [
    # Veo / Imagen / Vertex
    ("Veo 3 rate limit exceeded after 4 retries", "veo"),
    ("Veo 3 submission failed after 4 retries: quota", "veo"),
    # Timeout de Veo cae en "veo" (más accionable que timeout genérico)
    ("Veo 3 operation timed out after 10 min", "veo"),
    ("Veo response had no videos: {}", "veo"),
    ("Imagen 4 rate limit exceeded after all retries", "veo"),
    ("No background available. Check Veo 3 API or add videos to assets/backgrounds/", "veo"),
    # Render / ffmpeg
    ("ffprobe failed on render: error reading stream", "render"),
    ("rendered mp4 has no video stream", "render"),
    ("rendered mp4 lacks faststart (moov not at front)", "render"),
    ("verify: final.mp4 missing on disk after render", "render"),
    ("verify: final.mp4 is 0 bytes (truncated / empty)", "render"),
    ("Cannot load background video: moov atom not found", "render"),
    # Upload / R2
    ("Failed to fetch input from R2: NoSuchKey", "upload"),
    ("Could not download source audio from R2", "upload"),
    ("Could not download cached background from R2", "upload"),
    ("Source audio not available locally and no R2 key", "upload"),
    # Validación / content policy
    ("Content policy violation detected: explicit imagery", "validation"),
    ("UMG validation failed: missing ISRC; bad frame rate", "validation"),
    ("UMG delivery requested without umg_spec", "validation"),
    # Timing / transcripción
    ("Whisper transcription returned no segments", "timing"),
    ("Job abc123 has no persisted segments — cannot edit", "timing"),
    # Edit pipeline prefix: el inner message es el que clasifica
    ("Edit failed: ffprobe failed on render", "render"),
    ("Edit failed: Veo 3 rate limit exceeded after 4 retries", "veo"),
    # Reaper (mensaje customer-facing en español)
    ("El video se interrumpió por un problema temporal del servidor y no pudo completarse.", "reaper"),
    # Timeout genérico (sin mención a Veo)
    ("operation timed out waiting for worker", "timeout"),
    # Desconocido
    ("something completely unexpected happened", UNKNOWN),
    ("", UNKNOWN),
    (None, UNKNOWN),
]


@pytest.mark.parametrize("message,expected", CASES)
def test_classify_error(message, expected):
    assert classify_error(message) == expected


def test_all_categories_are_valid():
    """Toda categoría que devuelve el clasificador está en CATEGORIES."""
    for message, expected in CASES:
        assert classify_error(message) in CATEGORIES


def test_classify_never_raises():
    """El clasificador nunca lanza, ni con inputs raros."""
    for weird in (None, "", 0, 123, {"a": 1}, ["x"], Exception("boom")):
        result = classify_error(weird)
        assert result in CATEGORIES


def test_public_error_never_leaks_raw_exception_details():
    secret = "https://r2.example/file?X-Amz-Signature=super-secret /tmp/customer.wav"
    code, message = public_error(RuntimeError(secret), context="pipeline")
    assert code == "pipeline_unknown"
    assert "super-secret" not in message
    assert "/tmp/customer.wav" not in message
