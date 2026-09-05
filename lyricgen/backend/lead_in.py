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
    """
    hold = hold_seconds() if hold_s is None else max(0.0, float(hold_s))
    if not segs or hold <= 0.0:
        return segs
    try:
        out: list[dict] = []
        moved = 0
        for i, seg in enumerate(segs):
            new = dict(seg)
            try:
                end = float(seg.get("end", 0.0))
                if i + 1 < len(segs):
                    nxt = float(segs[i + 1].get("start", end))
                    target = min(end + hold, nxt - _MIN_GAP_S)
                    if target > end:
                        new["end"] = round(target, 3)
                        moved += 1
            except (TypeError, ValueError):
                pass
            out.append(new)
        if moved:
            logger.info("[LEAD_IN] %d/%d ends extendidos (hold=%.2fs)",
                        moved, len(segs), hold)
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
