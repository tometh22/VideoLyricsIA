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


def lead_seconds() -> float:
    """Lead configurado, saneado. 0 (default) = apagado; negativos = 0."""
    raw = os.environ.get("LYRIC_LEAD_IN_S", "0")
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        logger.warning("[LEAD_IN] LYRIC_LEAD_IN_S=%r inválido — apagado", raw)
        return 0.0


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
