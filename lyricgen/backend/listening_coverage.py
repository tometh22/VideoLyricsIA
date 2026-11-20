"""Cobertura de escucha por línea: qué líneas sonaron de verdad en el editor.

Matemática pura sobre el evento ``editor_audio_played`` (pares
``[inicio_ms, fin_ms]`` que el ``<audio>`` del editor reprodujo, ver
``frontend/src/lib/playbackCoverage.js``). La usan el reporte
``scripts/report_listening_coverage.py`` y el resumen previo a publicar
(``publish_review``). Nunca mira el texto de la letra: de cada línea sólo usa
``start``/``end``.
"""
from __future__ import annotations

import math

PLAYBACK_EVENT = "editor_audio_played"
HEARTBEAT_EVENT = "editor_activity_heartbeat"
# Una línea cuenta como escuchada si sonó al menos la mitad de su tramo.
DEFAULT_LINE_MIN_FRACTION = 0.5


def merge_intervals(ranges) -> list[tuple[int, int]]:
    """Unión de intervalos ``[inicio_ms, fin_ms]``; descarta los inválidos."""
    clean = []
    for pair in ranges or []:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            continue
        start, end = pair
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in pair):
            continue
        if not (math.isfinite(start) and math.isfinite(end)) or end <= start:
            continue
        clean.append((int(round(start)), int(round(end))))
    clean.sort()
    merged: list[list[int]] = []
    for start, end in clean:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def segment_span(segment) -> tuple[int, int] | None:
    """``(start_ms, end_ms)`` de una línea con duración; ``None`` si no tiene."""
    if not isinstance(segment, dict):
        return None
    try:
        start = float(segment.get("start"))
        end = float(segment.get("end"))
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(start) and math.isfinite(end)) or end <= start:
        return None
    return int(round(start * 1000)), int(round(end * 1000))


def line_spans(segments) -> list[tuple[int, int]]:
    """``(start_ms, end_ms)`` de cada línea con duración; ignora el texto."""
    return [span for span in (segment_span(s) for s in segments or []) if span is not None]


def covered_ms(span: tuple[int, int], merged: list[tuple[int, int]]) -> int:
    """Milisegundos de ``span`` cubiertos por intervalos ya fusionados."""
    start, end = span
    total = 0
    for range_start, range_end in merged:
        if range_end <= start:
            continue
        if range_start >= end:
            break
        total += min(end, range_end) - max(start, range_start)
    return total


def line_heard(span: tuple[int, int], merged: list[tuple[int, int]],
               line_min_fraction: float = DEFAULT_LINE_MIN_FRACTION) -> bool:
    return covered_ms(span, merged) >= line_min_fraction * (span[1] - span[0])
