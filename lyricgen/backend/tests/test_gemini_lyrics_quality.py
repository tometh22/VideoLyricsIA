"""Tests para la validación de quality del Gemini lyrics output.

`_fetch_lyrics_via_gemini_search` ahora rechaza output donde el
average chars/line supera 50 (signature de merged-stanza scraping).
El caller cae al path Whisper-sin-hint en vez de contaminar.

Caso real que motivó el fix: Noches Sin Sueño (Rata Blanca, 2026-05-12).
Gemini devolvió 439 chars / 12 lines (36.6 cpl) — pasó el umbral pero
visualmente era merged-2-line. El umbral 50 cpl es conservador para
captar los casos peores; el resto se cubre con el frontend auto-split
(PR siguiente).
"""
from unittest.mock import patch, MagicMock

import pytest


def _make_response(text):
    """Mock response object con la shape que Gemini SDK devuelve."""
    resp = MagicMock()
    resp.text = text
    cand = MagicMock()
    cand.finish_reason = "STOP"
    # Source URLs needed para pasar el grounding check
    chunk = MagicMock()
    chunk.web = MagicMock()
    chunk.web.uri = "https://www.letras.com/some-song"
    chunk.web.title = "Some Song Lyrics — Letras"
    cand.grounding_metadata = MagicMock()
    cand.grounding_metadata.grounding_chunks = [chunk]
    resp.candidates = [cand]
    return resp


def _call_gemini_fetch(text):
    """Helper: corre `_fetch_lyrics_via_gemini_search` con Gemini mockeado
    devolviendo `text`. Devuelve lo que la función retorna (string o None)."""
    from pipeline import _fetch_lyrics_via_gemini_search

    with patch("pipeline._get_genai_client") as mock_client:
        mock_client.return_value.models.generate_content.return_value = _make_response(text)
        return _fetch_lyrics_via_gemini_search(
            artist="Test Artist",
            song="Test Song",
            job_id=None,
            db=None,  # No cache write to skip
        )


# ─── Normal line lengths (accepted) ─────────────────────────────────

def test_gemini_normal_line_lengths_accepted():
    """Letra típica de pop/rock: líneas de 20-40 chars, separadas con \\n."""
    text = (
        "Caminando por la calle\n"
        "Vi tu cara reflejada\n"
        "En el cristal de la tienda\n"
        "Y me acordé de ti\n"
        "Como si nada\n"
        "Fueran los años\n"
        "Como si todo\n"
        "Siguiera igual\n"
        "Y aunque pase el tiempo\n"
        "Yo te recuerdo\n"
    )
    result = _call_gemini_fetch(text)
    assert result is not None, "Normal-length lyric text must be accepted"
    # Backend strips trailing whitespace; comparar sin el \n final
    assert result.strip() == text.strip()


def test_gemini_short_punchy_lines_accepted():
    """Reggaeton / hip-hop con líneas cortas (8-15 chars) — caso extremo bajo
    pero válido. No debe rechazar."""
    text = (
        "Tú lo sabes\n"
        "Yo lo sé\n"
        "Esta noche\n"
        "Va a arder\n"
        "Bésame\n"
        "Tócame\n"
        "Hazme tuya\n"
        "Una vez\n"
        "Solo una\n"
        "Para siempre\n"
        "Vamos vamos\n"
        "No te vayas\n"
    )
    result = _call_gemini_fetch(text)
    assert result is not None, "Short-line lyrics must be accepted"


# ─── Merged lines (rejected) ────────────────────────────────────────

def test_gemini_merged_lines_rejected():
    """Texto con líneas-párrafo (60+ chars cada una) debe rechazarse — es
    signature de merged-stanza scraping. Threshold del fix es 50 cpl."""
    text = (
        "Siento el calor de toda tu piel en mi cuerpo otra vez en esta noche fría\n"
        "Habla de vos me recuerda tus caricias y los momentos que pasamos juntos\n"
        "Son las noches que no paso a tu lado las que me hacen extrañarte más\n"
        "Que comprendo nena cuánto hay en nuestro amor que busca ser sagrado\n"
        "Quiero beber de tu esencia tan distinta y comprender lo que no tiene la mía\n"
        "Romperé la noche gritando tu nombre hasta que mi voz te llegará al corazón\n"
        "Una canción te busca a pesar de todo una canción te grita con toda su voz\n"
        "Que quizás no tengas un hombre perfecto que cuide cada paso que des\n"
    )
    result = _call_gemini_fetch(text)
    assert result is None, "Merged-line lyrics must be rejected (avg cpl > 50)"


def test_gemini_extreme_paragraphs_rejected():
    """Caso muy extremo: cada 'línea' es un párrafo entero (lyric site
    en flow mode). avg cpl >>> 50."""
    text = (
        "Siento el calor de toda tu piel en mi cuerpo otra vez Estrella fugaz enciende mi sed Misteriosa mujer Con tu amor sensual cuánto me das\n"
        "Haz que mi sueño sea una verdad Dame tu alma hoy haz el ritual Llévame al mundo donde pueda soñar Uh debo saber si en verdad En algún lado estás\n"
        "Voy a buscar una señal una canción Uh debo saber si en verdad En algún lado estás Sólo el amor que tú me das me ayudará\n"
        "Al amanecer tu imagen se va Misteriosa mujer Dejaste en mí lujuria total Hermosa y sensual Corazón sin Dios dame un lugar\n"
        "En ese mundo tibio casi irreal Deberé buscar una señal En aquel camino por el que vas Uh debo saber si en verdad\n"
        "En algún lado estás Voy a buscar una señal una canción Uh debo saber si en verdad En algún lado estás Sólo el amor que tu me das me ayudará\n"
        "Tu presencia marcó en mi vida el amor lo sé Es difícil pensar en vivir ya sin vos Corazón sin Dios dame un lugar\n"
        "En ese mundo tibio casi irreal Uh debo saber si en verdad En algún lado estás\n"
    )
    result = _call_gemini_fetch(text)
    assert result is None, "Paragraph-style lyrics must be rejected"


# ─── Edge cases ─────────────────────────────────────────────────────

def test_threshold_boundary_just_under_50_accepted():
    """avg cpl ≈ 45-49 (justo bajo el threshold) debe pasar. Threshold debe
    ser estricto > 50, no >= 50. Cada línea distinta para no disparar
    el repetition guard existente."""
    text = "\n".join(
        f"Esta es la línea numero {i:02d} con texto prueba_{i:02d}"
        for i in range(10)
    )
    lines = [l for l in text.split("\n") if l.strip()]
    avg_cpl = sum(len(l) for l in lines) / len(lines)
    assert 40 < avg_cpl < 50, f"Test setup off: avg cpl = {avg_cpl}"
    result = _call_gemini_fetch(text)
    assert result is not None, f"Just under threshold (cpl={avg_cpl}) must pass"


def test_threshold_boundary_just_over_50_rejected():
    """avg cpl ≈ 51-55 (justo sobre el threshold) debe rechazar.
    Líneas distintas para no disparar repetition guard."""
    text = "\n".join(
        f"Línea número {i:02d} con bastante texto para superar el threshold cpl"
        for i in range(10)
    )
    lines = [l for l in text.split("\n") if l.strip()]
    avg_cpl = sum(len(l) for l in lines) / len(lines)
    assert avg_cpl > 50, f"Test setup off: avg cpl = {avg_cpl}"
    result = _call_gemini_fetch(text)
    assert result is None, f"Just over threshold (cpl={avg_cpl}) must reject"


def test_gemini_lyrics_not_found_still_returns_none():
    """LYRICS_NOT_FOUND sentinel (caso existente) sigue devolviendo None."""
    result = _call_gemini_fetch("LYRICS_NOT_FOUND")
    assert result is None


def test_gemini_too_few_lines_still_rejected():
    """Texto con <8 líneas (caso existente) sigue devolviendo None."""
    text = "línea uno\nlínea dos\nlínea tres\n"  # solo 3 líneas
    result = _call_gemini_fetch(text)
    assert result is None
