"""Testigo whisper-1 contra la pantalla, también en grabaciones de estudio.

POR QUÉ EXISTE
--------------
El testigo independiente (whisper-1 sin prompt sobre la canción entera) ya
se paga y se congela en ``machine_evidence`` en cada canción, pero sólo los
vivos lo comparan contra la letra mostrada: ``main._maybe_word_vote`` lo
pasa como ``_independent_asr_words`` únicamente con ``_live_verify``, y de
ahí salen ``independent_text_mismatch`` / ``independent_uncovered_asr`` en
las "Partes a revisar". En estudio el testigo se guarda y nadie lo mira.

Medido el 16-11-2026 sobre las 15 demos (12 de estudio): la misma prueba
(``audio_coverage.text_mismatches`` < 0,4 y ``uncovered_spans``) marca sólo
Mc Kevinho — una repetición omitida que dejó la última línea ~4 s antes — y
la cola mal oída de Toni Garrido. Ninguna otra señal las veía.

QUÉ HACE
--------
Devuelve puntos de la Revisión rápida (``lyric_review``) de tipo "Escuchá
este tramo": nunca obligatorios, nunca con un arreglo aplicable. Se calcula
al abrir el editor sobre la letra ACTUAL, así que lo que el revisor arregla
deja de aparecer, y no toca la transcripción, el veredicto de calidad, las
ventanas que disparan reintentos pagos ni el render.

Los vivos quedan igual: si el pipeline ya comparó el testigo (``is_live`` o
``independent_witness_words`` en el veredicto congelado), no se agrega nada.

``WITNESS_STUDIO_COMPARE_MODE``: ``observe`` (default) muestra los puntos;
``off`` los apaga. Cualquier otro valor es ``off``. Puro, sin I/O.
"""
from __future__ import annotations

import hashlib
import os
from typing import Any

from heard_words import aligned_opcodes, norm_token, opcode_similarity

# Mismos umbrales que el camino en vivo (transcription_worker /
# transcription_quality): text_mismatches < 0,4 con su padding de 0,3 s, y
# huecos de uncovered_spans con sus defaults (≥ 3 s, ≥ 3 palabras).
MISMATCH_PAD_S = 0.3
# El veredicto en vivo declara el testigo inutilizable con menos de 8 palabras.
MIN_WITNESS_WORDS = 8
# lyric_review descarta al testigo como oído por debajo de esta similitud
# global con la pantalla: es otra versión o una alucinación.
MIN_WITNESS_SIMILARITY = 0.35
# Tope defensivo: si el testigo se desarma, no inunda la cola del revisor.
MAX_ITEMS = 12
_HEARD_MAX_CHARS = 90

KIND_MISMATCH = "witness_mismatch"
KIND_UNCOVERED = "witness_uncovered"


def mode() -> str:
    value = os.environ.get("WITNESS_STUDIO_COMPARE_MODE", "observe").strip().lower()
    return value if value in {"off", "observe"} else "off"


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _mmss(seconds: float) -> str:
    seconds = max(0.0, seconds)
    return f"{int(seconds // 60)}:{int(seconds % 60):02d}"


def witness_already_compared(machine_evidence: Any) -> bool:
    """True cuando el pipeline ya comparó el testigo (camino en vivo)."""
    if not isinstance(machine_evidence, dict):
        return False
    capture = machine_evidence.get("capture")
    if isinstance(capture, dict) and capture.get("independent_present"):
        return True
    decisions = machine_evidence.get("decisions")
    quality = decisions.get("quality") if isinstance(decisions, dict) else None
    metrics = quality.get("metrics") if isinstance(quality, dict) else None
    if not isinstance(metrics, dict):
        return False
    return bool(metrics.get("is_live")) or _f(metrics.get("independent_witness_words")) > 0


def sanitize(words: list[dict]) -> list[dict]:
    """Mismo filtro que el testigo en vivo: un whisper-1 ciego de canción
    entera inventa créditos de subtítulos sobre los instrumentales, y eso no
    es evidencia de canto."""
    try:
        from gap_rescue import _agrupar_en_lineas
        from pipeline import _is_whisper_hallucination
    except Exception:  # pragma: no cover - sin el filtro, mejor no acusar
        return []
    return [
        word
        for group in _agrupar_en_lineas(words or [])
        if not _is_whisper_hallucination(
            " ".join(str(w.get("word") or "") for w in group)
        )
        for word in group
    ]


def _heard(words: list[dict], start: float, end: float) -> str:
    text = " ".join(
        str(w.get("word") or "").strip() for w in words
        if start <= (_f(w.get("start")) + _f(w.get("end"))) / 2 <= end
    ).strip()
    if len(text) > _HEARD_MAX_CHARS:
        text = text[:_HEARD_MAX_CHARS].rsplit(" ", 1)[0] + "…"
    return text


def _similarity(segments: list[dict], words: list[dict]) -> float:
    screen = [t for s in segments for t in (norm_token(x) for x in str(s.get("text") or "").split()) if t]
    heard = [t for t in (norm_token(str(w.get("word") or "")) for w in words) if t]
    if not screen or not heard:
        return 0.0
    return opcode_similarity(aligned_opcodes(screen, heard), len(screen), len(heard))


def _key(kind: str, *parts: str) -> str:
    digest = hashlib.sha1("|".join(norm_token(p) for p in parts).encode("utf-8")).hexdigest()[:16]
    return f"{kind}:{digest}"


def _seg_id(segment: dict) -> str | None:
    value = segment.get("segment_id")
    return str(value) if value else None


def review_items(segments: list[dict], witness: list[dict],
                 machine_evidence: Any = None) -> list[dict]:
    """Puntos "Escuchá este tramo" del testigo contra la letra actual.

    Formato de ``lyric_review._finalize`` con ``occurrences`` ya armadas: el
    arreglo es ``inspect_witness``, que el editor no sabe aplicar (sólo
    reproduce el tramo y deja descartarlo)."""
    if mode() == "off" or witness_already_compared(machine_evidence):
        return []
    segments = [s for s in (segments or []) if isinstance(s, dict)]
    words = sanitize([w for w in (witness or []) if isinstance(w, dict)])
    if not segments or len(words) < MIN_WITNESS_WORDS:
        return []
    if _similarity(segments, words) < MIN_WITNESS_SIMILARITY:
        return []

    from audio_coverage import text_mismatches, uncovered_spans

    items: list[dict] = []
    for found in text_mismatches(segments, words, pad=MISMATCH_PAD_S):
        index = found["index"]
        line_text = str(segments[index].get("text") or "")
        heard = _heard(words, found["start"] - MISMATCH_PAD_S, found["end"] + MISMATCH_PAD_S)
        items.append({
            "kind": KIND_MISMATCH, "line": index, "required": False, "listen": True,
            "key": _key(KIND_MISMATCH, line_text, heard),
            "title": "No suena a lo que se canta",
            "why": (f"El testigo independiente oyó «{heard}» en esta línea. "
                    "Escuchá si dice lo que se canta, si falta una repetición "
                    "o si la línea quedó corrida de lugar."),
            "sources": {"witness"}, "action": "Escuchar", "dismiss": "Está bien así",
            "occurrences": [{
                "line_index": index, "line_segment_id": _seg_id(segments[index]),
                "start": round(_f(found["start"]), 2), "end": round(_f(found["end"]), 2),
                "before": line_text, "after": line_text,
                "fix": {"type": "inspect_witness", "text": heard},
            }],
        })
    for start, end, count in uncovered_spans(segments, words):
        before = [i for i, s in enumerate(segments) if _f(s.get("start")) < start]
        index = before[-1] if before else 0
        heard = _heard(words, start, end)
        items.append({
            "kind": KIND_UNCOVERED, "line": index, "required": False, "listen": True,
            "key": _key(KIND_UNCOVERED, str(segments[index].get("text") or ""), heard),
            "title": "Se canta sin línea en pantalla",
            "why": (f"Entre {_mmss(start)} y {_mmss(end)} el testigo independiente "
                    f"oyó {count} palabras que ninguna línea muestra. Escuchá si "
                    "falta una repetición o si una línea quedó corrida de lugar."),
            "sources": {"witness"}, "action": "Escuchar", "dismiss": "No se canta",
            "occurrences": [{
                "line_index": index, "line_segment_id": _seg_id(segments[index]),
                "start": round(start, 2), "end": round(end, 2),
                "before": heard, "after": heard,
                "fix": {"type": "inspect_witness", "text": heard},
            }],
        })
    items.sort(key=lambda item: item["occurrences"][0]["start"])
    return items[:MAX_ITEMS]
