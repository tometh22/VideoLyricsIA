"""Tarifas por llamada derivadas de la factura, no estimadas a mano.

El problema con la tabla de tarifas
-----------------------------------
`provenance.COST_PER_CALL` tenía Veo 3.1 Fast a $0,80 (= $0,10/s × 8s de
lista). Contra las facturas reales:

| mes | llamadas Veo medidas | factura Google | tarifa real |
|-----|---------------------:|---------------:|------------:|
| jun-2026 | 282 | $199,53 | ~$0,65 |
| jul-2026 | 487 | $313,00 | ~$0,62 |

O sea que el panel sobreestimaba ~25%, y ese error se propagaba a TODO:
costo por canción, margen por tenant, el tamaño del desperdicio. Peor: una
tarifa de lista nunca refleja descuentos, créditos ni cambios de precio del
proveedor, así que el error crece en silencio.

Ningún proveedor de IA devuelve el costo en la respuesta de la llamada. La
única fuente real es la factura. Este módulo cierra ese círculo:

    tarifa real = costo facturado del SKU  ÷  llamadas facturables medidas

Las llamadas salen de `ai_provenance` con el mismo `billable_filter()` que
usa el panel, así que numerador y denominador hablan del mismo universo.

Por qué esto es mejor que "arreglar el número"
----------------------------------------------
Cambiar $0,80 por $0,62 a mano habría quedado viejo el mes que Google toque
el precio o que cambie el mix de modelos. Esto se recalcula solo con cada
factura, y cuando NO hay factura el sistema usa la estimación de siempre y
lo dice — nunca inventa precisión que no tiene.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import func

logger = logging.getLogger("genly.rates")

# Herramienta -> prefijo de `tool_name` en ai_provenance.
TOOL_PREFIXES: dict[str, tuple[str, ...]] = {
    "veo": ("veo-",),
    "imagen": ("imagen-",),
    "gemini": ("gemini-",),
}

# Una tarifa derivada de pocas llamadas es ruido, no medición: un mes con 3
# llamadas y un cargo mínimo daría una tarifa absurda que después se aplica
# a miles. Por debajo de esto se ignora la calibración y se usa la tabla.
MIN_CALLS_FOR_CALIBRATION = 25

# Cinturón de seguridad: si la tarifa derivada se va más de esto respecto de
# la estimada, casi seguro el SKU está mal mapeado (por ejemplo, la línea de
# Veo capturando también almacenamiento). Se reporta pero no se aplica.
MAX_PLAUSIBLE_DRIFT = 5.0


def _billable_calls_by_model(db, start: datetime, end: datetime) -> dict[str, int]:
    """Llamadas facturables por `tool_name` exacto en la ventana.

    Se guarda el nombre completo del modelo, no sólo la herramienta, porque
    la estimación de referencia hay que ponderarla por los modelos que de
    verdad se usaron. `COST_PER_CALL` tiene Veo 2.0 a $4,00 y Veo 3.1 Fast a
    $0,80; quedarse con el máximo del grupo compara la tarifa real contra un
    modelo legacy que hace meses no se llama, y la real sale "implausible"
    cuando en realidad está bien.
    """
    from database import AIProvenance
    from provenance import billable_filter

    rows = (
        db.query(AIProvenance.tool_name, func.count(AIProvenance.id))
        .filter(AIProvenance.created_at >= start, AIProvenance.created_at < end)
        .filter(billable_filter())
        .group_by(AIProvenance.tool_name)
        .all()
    )
    out: dict[str, int] = {}
    for tool_name, n in rows:
        name = (tool_name or "").lower()
        if any(name.startswith(p)
               for prefixes in TOOL_PREFIXES.values() for p in prefixes):
            out[name] = out.get(name, 0) + int(n)
    return out


def _tool_of(model_name: str) -> str | None:
    low = (model_name or "").lower()
    for tool, prefixes in TOOL_PREFIXES.items():
        if any(low.startswith(p) for p in prefixes):
            return tool
    return None


def derive_rates(sessions: dict, invoiced_by_tool: dict[str, float],
                 period: str) -> dict:
    """Tarifa real por llamada para un mes.

    `sessions` mapea nombre de entorno -> sesión. **Tienen que ir los dos**
    (staging y prod): comparten el mismo proyecto de GCP, así que la factura
    cubre ambos y contar uno solo inflaría la tarifa al doble.

    `invoiced_by_tool` es la salida de `billing_sources.gcp_cost_by_tool`.
    """
    from cost_attribution import period_bounds

    start, end = period_bounds(period)
    by_model: dict[str, int] = {}
    for db in sessions.values():
        for model, n in _billable_calls_by_model(db, start, end).items():
            by_model[model] = by_model.get(model, 0) + n

    calls: dict[str, int] = {}
    for model, n in by_model.items():
        tool = _tool_of(model)
        if tool:
            calls[tool] = calls.get(tool, 0) + n

    # Estimación de referencia PONDERADA por los modelos realmente usados.
    # Ver `_billable_calls_by_model`: quedarse con el máximo del grupo
    # compara contra un modelo legacy que ya no se llama.
    from provenance import cost_for_record

    weighted: dict[str, list[float]] = {}
    for model, n in by_model.items():
        tool = _tool_of(model)
        if not tool:
            continue
        rate = cost_for_record(model, "google_vertex")
        acc = weighted.setdefault(tool, [0.0, 0.0])
        acc[0] += rate * n
        acc[1] += n
    estimated = {t: round(s / c, 6) for t, (s, c) in weighted.items() if c}

    results = []
    applied: dict[str, float] = {}
    for tool in sorted(set(calls) | set(invoiced_by_tool)):
        n = calls.get(tool, 0)
        billed = float(invoiced_by_tool.get(tool, 0.0))
        est = estimated.get(tool)
        derived = round(billed / n, 6) if (n and billed) else None

        status, reason = "ok", None
        if not billed:
            status, reason = "sin_factura", "el SKU no aparece en la factura"
        elif n < MIN_CALLS_FOR_CALIBRATION:
            status, reason = ("muestra_chica",
                              f"{n} llamadas (<{MIN_CALLS_FOR_CALIBRATION})")
        elif est and derived and (derived / est > MAX_PLAUSIBLE_DRIFT
                                  or est / derived > MAX_PLAUSIBLE_DRIFT):
            status, reason = ("implausible",
                              f"derivada ${derived} vs estimada ${est} — "
                              "revisar el mapeo de SKU")
        if status == "ok" and derived:
            applied[tool] = derived

        results.append({
            "tool": tool, "calls": n, "invoiced_usd": round(billed, 4),
            "derived_rate": derived, "estimated_rate": est,
            "drift": (round(derived / est, 3) if est and derived else None),
            "status": status, "reason": reason,
        })

    return {
        "period": period,
        "environments": sorted(sessions),
        "rates": results,
        "applied": applied,
        "note": (
            "tarifa real = facturado del SKU ÷ llamadas facturables medidas. "
            "Requiere los DOS entornos: comparten proyecto de GCP, así que "
            "contar uno solo duplicaría la tarifa."
        ),
    }


def store_rates(db, period: str, calibration: dict) -> int:
    """Persiste las tarifas aplicables como un CostSnapshot.

    Va en la tabla que ya existe con `source='rate_calibration'` en vez de
    crear una nueva: el motivo es el mismo que el de los snapshots de
    facturación — si el mes no se guarda, se pierde (las APIs sólo exponen
    una ventana móvil).
    """
    from database import CostSnapshot

    row = (
        db.query(CostSnapshot)
        .filter(CostSnapshot.period == period,
                CostSnapshot.source == "rate_calibration")
        .one_or_none()
    )
    if row is None:
        row = CostSnapshot(period=period, source="rate_calibration")
        db.add(row)
    applied = calibration.get("applied") or {}
    row.amount_usd = None          # no es un gasto, son tarifas
    row.status = "ok" if applied else "not_configured"
    row.detail = (f"{len(applied)} tarifa(s) derivadas de la factura"
                  if applied else "sin factura utilizable para calibrar")
    row.is_estimate = False
    row.breakdown = calibration.get("rates") or []
    row.fetched_at = datetime.now(timezone.utc)
    db.commit()
    return len(applied)


def load_applied_rates(db, period: str) -> dict[str, float]:
    """Tarifas calibradas de un mes, o {} si no hay.

    Nunca levanta: si la tabla no existe todavía o la fila está rota, el
    llamador cae a la tabla estimada. Una calibración ausente debe degradar
    a la estimación, jamás romper el panel.
    """
    try:
        from database import CostSnapshot

        row = (
            db.query(CostSnapshot)
            .filter(CostSnapshot.period == period,
                    CostSnapshot.source == "rate_calibration",
                    CostSnapshot.status == "ok")
            .one_or_none()
        )
        if not row or not row.breakdown:
            return {}
        return {
            r["tool"]: float(r["derived_rate"])
            for r in row.breakdown
            if r.get("status") == "ok" and r.get("derived_rate")
        }
    except Exception as exc:
        logger.warning("[RATES] no pude leer la calibración de %s: %r",
                       period, exc)
        return {}


def rate_for_tool(tool_name: str, calibrated: dict[str, float]) -> float | None:
    """Tarifa calibrada para un `tool_name`, o None si no hay."""
    low = (tool_name or "").lower()
    for tool, prefixes in TOOL_PREFIXES.items():
        if any(low.startswith(p) for p in prefixes):
            return calibrated.get(tool)
    return None
