"""Lead-in — mostrar cada línea un toque ANTES de que se cante.

WHY
---
Los aligners (whisperX / FA / CTC) marcan la línea en el onset acústico
exacto de la primera palabra — verificado contra la energía del stem
(2026-07-03: "Todo el dolor…" detectado a 33.65s, voz real a 33.64s).
Pero una línea que aparece exactamente cuando arranca la voz SE PERCIBE
tarde: el ojo necesita llegar antes que el oído. ROTOR lo trae de fábrica
(sus starts quedan ~0.1-0.2s antes del onset) y nuestros operadores lo
aplican A MANO: de 886 ajustes finos de inicio en las 40 canciones gold
de UMG, el 94% mueve la línea hacia antes, mediana −0.41s. Replay offline
del sweep (baseline + lead vs gold aprobado): 0.2-0.4s sube las líneas
"a menos de 0.3s del gold" de 34.8% → ~49%.

CONTRACT
--------
- Behind `LYRIC_LEAD_IN_S` (default 0 = apagado, comportamiento actual).
  Staging corre 0.4 para el A/B visual; el valor fino (0.2-0.4) se
  calibra con el operador.
- Solo mueve `start`, y solo hacia ANTES. `end` y los word-stamps quedan
  intactos — el highlight karaoke sigue pegado al onset real; lo único
  que se adelanta es la aparición de la línea.
- Clamp: nunca pisa el `end` de la línea anterior (queda un gap mínimo)
  y nunca cruza 0.
- `apply(segs)` devuelve segmentos nuevos o los originales ante cualquier
  falla. Never raises. Puro y unit-testeable.
"""

from __future__ import annotations

from contextlib import contextmanager
import contextvars
import logging
import os

logger = logging.getLogger("genly.lead_in")

# Gap mínimo con el fin de la línea anterior al clampear. Chico a
# propósito: si el cantante encadena frases, el lead disponible es ~0 y
# la línea queda donde estaba — el bias solo actúa donde hay aire.
_MIN_GAP_S = 0.01

# Tope duro del lead. Tres calibraciones independientes convergen en que
# un lead grande se percibe como DESincronización, no como anticipación:
#   - whisperx_transcribe.py bajó su lead de 120→80ms (2026-05-31) porque
#     los revisores UMG leían 120ms como "la línea cae antes que la voz".
#   - Rotor, medido línea-a-línea contra el job 6f4047db (28-07): sus
#     carteles aparecen ~0.07s antes del onset — no 0.4.
#   - El usuario reportó "partes mal sincronizadas" con staging en 0.4;
#     el desfase medido contra Rotor era exactamente el lead (−0.33s
#     sistemático en 17 líneas).
# El sweep del docstring (mediana de ajuste manual −0.41s) midió a los
# operadores corrigiendo el output SIN lead — incluye compensar retardo
# del aligner, no es un target de lead puro. Valores arriba del cap se
# clampean con warning en vez de aceptarse en silencio: 0.4 en staging
# fue exactamente ese footgun.
_MAX_LEAD_S = 0.15


def lead_seconds() -> float:
    """Lead configurado, saneado. 0 (default) = apagado; negativos = 0;
    valores por encima de `_MAX_LEAD_S` se clampean (con warning)."""
    raw = os.environ.get("LYRIC_LEAD_IN_S", "0")
    try:
        lead = max(0.0, float(raw))
    except (TypeError, ValueError):
        logger.warning("[LEAD_IN] LYRIC_LEAD_IN_S=%r inválido — apagado", raw)
        return 0.0
    if lead > _MAX_LEAD_S:
        logger.warning("[LEAD_IN] LYRIC_LEAD_IN_S=%.2f excede el tope %.2f — "
                       "clampeado (leads grandes se perciben como "
                       "desincronización)", lead, _MAX_LEAD_S)
        return _MAX_LEAD_S
    return lead


# Aire mínimo antes de la línea siguiente (LYRIC_MIN_GAP_MS). 0 = el
# comportamiento de siempre: el hold se clampea a siguiente − _MIN_GAP_S y
# nunca acorta. Con un valor mayor, el fin se recorta cuando hace falta para
# dejar ese aire (diagnóstico 2026-10-05: al editar un fin, el operador deja
# 0,3-0,4 s de aire). La prueba prospectiva fija el valor por job con
# `min_gap_override`, sin tocar el entorno.
_MIN_GAP_OVERRIDE_MS: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "lyric_min_gap_override_ms", default=None,
)
_GAP_STATS: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "lyric_min_gap_stats", default=None,
)
# Pisos del recorte. La última palabra puede quedar fuera de la tarjeta a lo
# sumo 0,10 s: por debajo del aviso LYRIC_END_BEFORE_WORD_END del preflight
# (0,12 s) y lejos del re-estirado de enforce_line_word_consistency (0,35 s).
# Ninguna línea queda más corta que el mínimo de normalize_segments_timing.
_WORD_FLOOR_S = 0.10
_MIN_LINE_S = 0.3


def min_gap_seconds() -> float:
    """Aire mínimo configurado, en segundos (0 = comportamiento actual)."""
    override = _MIN_GAP_OVERRIDE_MS.get()
    raw = override if override is not None else os.environ.get("LYRIC_MIN_GAP_MS", "0")
    try:
        return max(0, int(float(raw))) / 1000.0
    except (TypeError, ValueError):
        logger.warning("[LEAD_IN] LYRIC_MIN_GAP_MS=%r inválido — 0", raw)
        return 0.0


@contextmanager
def min_gap_override(gap_ms: int | None, stats: dict | None = None):
    """Fija el aire mínimo (y junta exposición) para el job en curso.

    Las ContextVar viajan con ``asyncio.run``, así que todos los ``polish``
    del job (el del emisor y el posterior al retime de CTC) ven el mismo
    valor sin cambiar sus firmas.
    """
    gap_token = _MIN_GAP_OVERRIDE_MS.set(gap_ms)
    stats_token = _GAP_STATS.set(stats)
    try:
        yield
    finally:
        _MIN_GAP_OVERRIDE_MS.reset(gap_token)
        _GAP_STATS.reset(stats_token)


def _last_word_end(seg: dict) -> float | None:
    ends = []
    for word in seg.get("words") or []:
        if not isinstance(word, dict):
            continue
        try:
            ends.append(float(word.get("end", word.get("start"))))
        except (TypeError, ValueError):
            continue
    return max(ends) if ends else None


def hold_seconds() -> float:
    """Hold configurado (LYRIC_HOLD_S), saneado. Default seguro = 0.5 s.

    El default vive también en ``observability.runtime_timing_config`` para
    que el readiness compare el valor *efectivo*, no sólo la presencia de la
    variable. Así, quitar accidentalmente LYRIC_HOLD_S de un servicio no
    vuelve a apagar el hold en silencio.
    """
    raw = os.environ.get("LYRIC_HOLD_S", "0.5")
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        logger.warning(
            "[LEAD_IN] LYRIC_HOLD_S=%r inválido — usando default seguro 0.5 s",
            raw,
        )
        return 0.5


def apply_hold(segs: list[dict], hold_s: float | None = None) -> list[dict]:
    """Extiende el `end` de cada línea hasta `hold_s`, con tope en el inicio
    de la línea siguiente (menos un gap mínimo).

    Medido contra el gold (03/07, sweep de holds sobre 40 canciones): los
    operadores NO empalman estilo ROTOR (78% de las transiciones aprobadas
    dejan aire; hold sin tope da 23.8% ≤0.3s vs 37.5% de hoy) pero un hold
    chico ayuda. El lote de agosto recalibró el fallback a 0.5s después de
    eliminar el auto-trim del editor; ese valor queda como fallback temporal,
    no como sustituto del endpoint derivado del canto. Nunca acorta; la última
    línea queda intacta (sin señal de gold para el outro, y el hold infinito
    de ROTOR es justo lo que los operadores no hacen).

    Con LYRIC_MIN_GAP_MS > 0 (o `min_gap_override`) el tope pasa a ser
    siguiente − aire, y además se recorta el fin cuando ya invade ese aire,
    sin bajar de última palabra − 0,10 s ni de inicio + 0,3 s, y sin tocar
    líneas bloqueadas. Con 0 el comportamiento es el de siempre.
    """
    hold = hold_seconds() if hold_s is None else max(0.0, float(hold_s))
    gap = max(_MIN_GAP_S, min_gap_seconds())
    trim = gap > _MIN_GAP_S
    if not segs or (hold <= 0.0 and not trim):
        return segs
    stats = _GAP_STATS.get()
    try:
        out: list[dict] = []
        moved = trimmed = floored = 0
        for i, seg in enumerate(segs):
            new = dict(seg)
            try:
                end = float(seg.get("end", 0.0))
                if i + 1 < len(segs):
                    nxt = float(segs[i + 1].get("start", end))
                    target = min(end + hold, nxt - gap)
                    if target > end:
                        new["end"] = round(target, 3)
                        moved += 1
                    elif trim and target < end and not (
                        seg.get("locked") or seg.get("operator_locked")
                    ):
                        start = float(seg.get("start", 0.0))
                        # Solapes grandes (armonías, coros encimados): no tocar.
                        if nxt > start + _MIN_LINE_S:
                            floor = start + _MIN_LINE_S
                            last_word = _last_word_end(seg)
                            if last_word is not None:
                                floor = max(floor, last_word - _WORD_FLOOR_S)
                            new_end = max(target, floor)
                            if new_end < end - 0.001:
                                new["end"] = round(new_end, 3)
                                trimmed += 1
                                floored += new_end > target + 0.001
            except (TypeError, ValueError):
                pass
            out.append(new)
        if moved or trimmed:
            logger.info("[LEAD_IN] %d/%d ends extendidos, %d recortados (hold=%.2fs gap=%.2fs)",
                        moved, len(segs), trimmed, hold, gap)
        if stats is not None:
            stats["polish_calls"] = stats.get("polish_calls", 0) + 1
            stats["lines_seen"] = stats.get("lines_seen", 0) + len(segs)
            stats["lines_trimmed"] = stats.get("lines_trimmed", 0) + trimmed
            stats["lines_word_floor"] = stats.get("lines_word_floor", 0) + floored
        return out
    except Exception as e:  # pragma: no cover
        logger.warning("[LEAD_IN] apply_hold falló (%s) — segmentos originales", e)
        return segs


def polish(segs: list[dict]) -> list[dict]:
    """Punto de entrada único del pulido de presentación: lead + hold.

    Orden importa: primero el lead (mueve starts hacia antes), después el
    hold — así el tope del hold usa el start YA adelantado de la línea
    siguiente y la extensión nunca pisa una línea visible.
    """
    return apply_hold(apply(segs))


def apply(segs: list[dict], lead_s: float | None = None) -> list[dict]:
    """Adelanta el `start` de cada segmento hasta `lead_s`, clampeado.

    `lead_s=None` lee el env; pasarlo explícito es para tests/sweeps.
    """
    lead = lead_seconds() if lead_s is None else max(0.0, float(lead_s))
    if not segs or lead <= 0.0:
        return segs
    try:
        out: list[dict] = []
        # La primera línea no tiene anterior: su único piso es 0.
        prev_end = -_MIN_GAP_S
        moved = 0
        for seg in segs:
            new = dict(seg)
            try:
                start = float(seg.get("start", 0.0))
                target = max(0.0, start - lead, prev_end + _MIN_GAP_S)
                # Solo hacia antes: si el clamp cae después del start
                # original (líneas encadenadas / input solapado), no tocar.
                if target < start:
                    new["start"] = round(target, 3)
                    moved += 1
                prev_end = float(seg.get("end", start))
            except (TypeError, ValueError):
                # Segmento raro (start no numérico): pasarlo tal cual y
                # anclar el clamp del siguiente a lo que se pueda.
                pass
            out.append(new)
        if moved:
            logger.info("[LEAD_IN] %d/%d starts adelantados (lead=%.2fs)",
                        moved, len(segs), lead)
        return out
    except Exception as e:  # pragma: no cover — el render nunca se cae por esto
        logger.warning("[LEAD_IN] apply falló (%s) — segmentos originales", e)
        return segs
