"""Hold de lectura para las líneas que re-alinea `/jobs/{id}/reanchor`.

WHY
---
La subida termina cada línea en ``última palabra + LYRIC_HOLD_S`` (0,5 s,
``lead_in.polish`` dentro de ``_emit_segments``). El re-anclado del editor
("Re-sincronizar", pegar letra oficial) toma el timing de
``_maybe_anchor_align`` y lo persiste sin pasar por ese pulido, así que cada
línea re-alineada se apaga en el último fonema.

Medido el 19-11-2026 sobre las 201 canciones con pedidos de UMG
(sep-nov): de los pedidos "la línea se va antes de que termine la voz" cuyo
fin se puede rastrear, 39 de 47 terminaban exactamente en la última palabra,
y 35 de esos fines nacieron en una re-escritura del servidor (versión
``migration``: el re-anclado escribe ``segments_json`` y el puente del
editor la registra así). UMG Argentina pidió alargar el 8,0 % de esas líneas
sin hold contra el 1,1 % de las que traían el hold de 0,5 s.

CONTRACT
--------
- ``REANCHOR_HOLD_MODE``: ``off`` (default, comportamiento actual),
  ``observe`` (calcula y reporta cuántas líneas cambiarían, no toca nada) o
  ``apply``.
- Usa el MISMO ``lead_in.apply_hold`` (y por lo tanto ``LYRIC_HOLD_S`` y
  ``LYRIC_MIN_GAP_MS`` de la paridad de la flota): no introduce un valor de
  timing nuevo.
- Sólo alarga. Sólo líneas que este re-anclado re-timeó (su par inicio/fin
  no existía antes), que terminan en su última palabra (±20 ms) y que no
  están bloqueadas. Un fin que el revisor dejó a mano no se toca.
- Puro y sin excepciones hacia afuera: ante cualquier falla devuelve los
  segmentos originales.
"""
from __future__ import annotations

import logging
import os

import lead_in

logger = logging.getLogger("genly.reanchor_hold")

MODES = ("off", "observe", "apply")
# Tolerancia para "termina en la última palabra": los aligners escriben el
# fin de línea igual al fin de la última palabra; 20 ms cubre el redondeo a
# cuatro decimales del puente del editor.
_AT_LAST_WORD_S = 0.02
_LOCK_KEYS = ("locked", "operator_locked", "timing_locked")


def mode() -> str:
    value = os.environ.get("REANCHOR_HOLD_MODE", "off").strip().lower()
    return value if value in MODES else "off"


def _float(value) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None  # NaN fuera


def _last_word_end(seg: dict) -> float | None:
    ends = []
    for word in seg.get("words") or []:
        if isinstance(word, dict):
            end = _float(word.get("end"))
            if end is not None:
                ends.append(end)
    return max(ends) if ends else None


def _clock(seg: dict) -> tuple[float, float] | None:
    start, end = _float(seg.get("start")), _float(seg.get("end"))
    if start is None or end is None:
        return None
    return round(start, 3), round(end, 3)


def plan(segments: list[dict], previous: list[dict] | None = None,
         hold_s: float | None = None) -> tuple[list[dict], dict]:
    """Devuelve ``(segmentos_con_hold, stats)`` sin decidir si se aplican."""
    stats = {"lines": len(segments or []), "eligible": 0, "extended": 0,
             "seconds_added": 0.0, "hold_s": None}
    if not segments:
        return segments, stats
    try:
        hold = lead_in.hold_seconds() if hold_s is None else max(0.0, float(hold_s))
        stats["hold_s"] = hold
        before = {c for c in (_clock(s) for s in previous or [] if isinstance(s, dict)) if c}
        polished = lead_in.apply_hold(segments, hold)
        out: list[dict] = []
        for seg, pol in zip(segments, polished):
            if not isinstance(seg, dict):
                out.append(seg)
                continue
            end = _float(seg.get("end"))
            last = _last_word_end(seg)
            clock = _clock(seg)
            eligible = (
                end is not None and last is not None and clock is not None
                and clock not in before
                and not any(seg.get(key) for key in _LOCK_KEYS)
                and abs(end - last) <= _AT_LAST_WORD_S
            )
            if not eligible:
                out.append(seg)
                continue
            stats["eligible"] += 1
            new_end = _float(pol.get("end")) if isinstance(pol, dict) else None
            if new_end is None or new_end <= end + 0.001:
                out.append(seg)
                continue
            updated = dict(seg)
            updated["end"] = round(new_end, 4)
            out.append(updated)
            stats["extended"] += 1
            stats["seconds_added"] += new_end - end
        stats["seconds_added"] = round(stats["seconds_added"], 3)
        return out, stats
    except Exception as exc:  # pragma: no cover — nunca rompe el re-anclado
        logger.warning("[REANCHOR_HOLD] plan falló (%s) — segmentos originales", exc)
        return segments, stats


def run(segments: list[dict], previous: list[dict] | None = None) -> tuple[list[dict], dict | None]:
    """Punto de entrada del re-anclado. ``off`` no calcula nada."""
    current = mode()
    if current == "off":
        return segments, None
    planned, stats = plan(segments, previous)
    stats["mode"] = current
    stats["applied"] = current == "apply" and stats["extended"] > 0
    if stats["eligible"]:
        logger.info("[REANCHOR_HOLD] mode=%s elegibles=%d alargadas=%d (+%.2fs, hold=%.2fs)",
                    current, stats["eligible"], stats["extended"],
                    stats["seconds_added"], stats["hold_s"] or 0.0)
    return (planned if stats["applied"] else segments), stats
