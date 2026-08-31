"""Read side of the daily cost panel — where the business rules live.

`cost_daily_collector` stores the provider's raw granularity; this module
turns it into the numbers a person reads. Everything that is a function of
the MONTH or of a policy decision happens here, on purpose:

* R2's free allowances (10 GB-month, 1M class-A, 10M class-B).
* Railway's `max(metered, plan_minimum)` floor.
* Which OpenAI line items count as ours — a moving target: July's
  `gpt-4o-mini` spend was somebody else's, August's is ours.

Applying any of those at collect time would freeze a decision into history
that the provider APIs can no longer let us revise.

THE COVERAGE GATE
-----------------
`series()` reports how many (day, source) cells it expected versus how many
it actually has, and REFUSES to divide by delivered videos when the answer
would be built on holes. A cost-per-video computed over a month missing two
days of Vertex is not "roughly right", it is a smaller number that looks
exactly like good news — which is the specific failure this panel exists to
prevent.
"""

from __future__ import annotations

import calendar
import logging
import os
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from database import CostCollectionRun, CostDaily

logger = logging.getLogger("genly.costs.series")

# Los mismos que colecta el colector. `github` no está: factura $0 y su API
# reporta ciclo de facturación, no días calendario.
SOURCES = ("gcp", "railway", "r2", "openai", "replicate", "fixed")

# Fuentes cuyo importe sale de una métrica propia y no de un importe que el
# proveedor haya facturado. El panel tiene que poder decir qué porción del
# total es factura y qué porción es modelo nuestro.
ESTIMATED_SOURCES = ("railway", "r2", "replicate")


def _openai_filter() -> list[str]:
    """Substrings de line_item que contamos como nuestros.

    Se lee en cada request a propósito: `billing_sources` lo tiene como
    constante de módulo y por eso cambiarlo exige reiniciar el proceso.
    """
    raw = os.environ.get("OPENAI_COST_LINE_ITEMS", "whisper,gpt-4o-mini")
    return [s.strip().lower() for s in raw.split(",") if s.strip()]


def _bucket_key(d: date, granularity: str) -> str:
    if granularity == "day":
        return d.isoformat()
    if granularity == "week":
        # ISO week: el lunes manda. Evita que "semana" signifique cosas
        # distintas según el día en que uno mire el panel.
        monday = d - timedelta(days=d.weekday())
        return monday.isoformat()
    return f"{d.year:04d}-{d.month:02d}"


def coverage(db: Session, since: date, until: date) -> dict:
    """Qué se pudo recolectar y qué no, en el rango pedido.

    `until` es inclusivo. El día de hoy se excluye del denominador: todavía
    está acumulando y nunca va a estar "completo".
    """
    hoy = datetime.now(timezone.utc).date()
    last = min(until, hoy - timedelta(days=1))
    if last < since:
        return {"expected_cells": 0, "collected_cells": 0, "complete": True,
                "missing": []}

    runs = {
        (r.day, r.source): r.status
        for r in db.query(CostCollectionRun)
        .filter(CostCollectionRun.day >= since, CostCollectionRun.day <= last).all()
    }
    missing, expected, collected = [], 0, 0
    d = since
    while d <= last:
        for source in SOURCES:
            expected += 1
            status = runs.get((d, source))
            if status in ("ok", "not_configured"):
                collected += 1
            else:
                missing.append({"day": d.isoformat(), "source": source,
                                "status": status or "nunca_intentado"})
        d += timedelta(days=1)
    return {
        "expected_cells": expected, "collected_cells": collected,
        "complete": not missing,
        # Cortado: una laguna larga no debe producir una respuesta de 10 MB.
        "missing": missing[:200], "missing_total": len(missing),
    }


def _apply_monthly_rules(db: Session, month_rows: dict, month: str) -> dict:
    """Franquicias y mínimos: reglas del MES, aplicadas una sola vez.

    Restar la franquicia de 1M de operaciones clase A por día da $0 todos
    los días aunque el mes pase el millón; restar 1/31 tampoco sirve porque
    el excedente no es lineal. Por eso se aplica acá, sobre el agregado.
    """
    ajustes = []

    # R2: franquicias mensuales
    if os.environ.get("R2_APPLY_FREE_TIER", "1") == "1":
        import billing_sources as bs
        gb_mes = month_rows.get(("r2", "storage"), {}).get("qty", 0.0)
        # `qty` de storage viene en GB por día; el GB-mes es el promedio.
        dias = month_rows.get("_dias", 1) or 1
        gb_promedio = gb_mes / dias
        libres = min(gb_promedio, bs.R2_FREE_GB)
        if libres > 0:
            ajustes.append({
                "concepto": "r2_franquicia_storage",
                "amount_usd": -round(libres * bs.R2_USD_PER_GB_MONTH, 4),
                "detail": f"{libres:.1f} GB-mes gratis de los {gb_promedio:.1f} usados",
            })
        for clase, libre_n, tarifa in (
            ("class_a", bs.R2_FREE_CLASS_A, bs.R2_USD_PER_MILLION_CLASS_A),
            ("class_b", bs.R2_FREE_CLASS_B, bs.R2_USD_PER_MILLION_CLASS_B),
        ):
            reqs = month_rows.get(("r2", clase), {}).get("qty", 0.0)
            libres_n = min(reqs, libre_n)
            if libres_n > 0:
                ajustes.append({
                    "concepto": f"r2_franquicia_{clase}",
                    "amount_usd": -round(libres_n / 1_000_000 * tarifa, 4),
                    "detail": f"{int(libres_n):,} de {int(reqs):,} requests gratis",
                })

    return {"month": month, "ajustes": ajustes,
            "total_ajustes": round(sum(a["amount_usd"] for a in ajustes), 4)}


def series(db: Session, since: date, until: date, *,
           granularity: str = "day", group_by: str = "source") -> dict:
    """Serie de costo agregada, con su propia cobertura al lado.

    `group_by`:
      source     — quién cobra (gcp, railway, ...)
      behavior   — fijo / variable / stock. Es el corte que importa para
                   decidir: el promedio por video baja al subir el volumen
                   aunque la ganancia absoluta caiga, y sólo separando el
                   piso fijo del marginal se ve eso.
      sku        — el detalle fino dentro de cada proveedor.
    """
    if granularity not in ("day", "week", "month"):
        raise ValueError("granularity debe ser day|week|month")
    if group_by not in ("source", "behavior", "sku"):
        raise ValueError("group_by debe ser source|behavior|sku")

    cov = coverage(db, since, until)

    # Sólo UNA dimensión por (día, fuente): `total` y `sku` son el mismo
    # dinero visto de dos formas, y sumarlos multiplica el resultado.
    #
    # Para `behavior` hay que preferir `sku`: el comportamiento no es
    # uniforme dentro de una fuente. R2 mezcla storage (stock, crece con
    # cada entrega y nunca baja) con operaciones (variable), y la fila
    # `total` no puede llevar las dos etiquetas.
    if group_by == "sku":
        # `line_item` es el "sku" de OpenAI: si se pidiera sólo dim_type='sku',
        # OpenAI desaparecería del desglose fino sin ningún aviso, y el total
        # de la vista por SKU no coincidiría con el de la vista por fuente.
        filas = (db.query(CostDaily)
                 .filter(CostDaily.day >= since, CostDaily.day <= until,
                         CostDaily.dim_type.in_(("sku", "line_item"))).all())
    elif group_by == "behavior":
        crudas = (db.query(CostDaily)
                  .filter(CostDaily.day >= since, CostDaily.day <= until,
                          CostDaily.dim_type.in_(("total", "sku"))).all())
        con_sku = {(f.day, f.source) for f in crudas if f.dim_type == "sku"}
        filas = [f for f in crudas
                 if (f.dim_type == "sku") or ((f.day, f.source) not in con_sku)]
    else:
        filas = (db.query(CostDaily)
                 .filter(CostDaily.day >= since, CostDaily.day <= until,
                         CostDaily.dim_type == "total").all())
    dim_needed = "sku" if group_by == "sku" else "total"

    of = _openai_filter()
    buckets: dict[str, dict[str, float]] = {}
    totales: dict[str, float] = {}
    facturado = estimado = 0.0

    for f in filas:
        if f.amount_usd is None:
            continue
        # Los line_item de OpenAI se filtran acá, no al colectar. Para el
        # agregado por fuente el `total` de OpenAI trae la org entera, así
        # que hay que reconstruirlo desde los line_item.
        if f.source == "openai" and dim_needed == "total":
            continue
        if f.source == "openai" and f.dim_type == "line_item":
            if of and not any(s in (f.dim_value or "").lower() for s in of):
                continue

        key = _bucket_key(f.day, granularity)
        if group_by == "source":
            g = f.source
        elif group_by == "behavior":
            g = f.cost_behavior or "sin_clasificar"
        else:
            g = f"{f.source}:{f.dim_value}"
        buckets.setdefault(key, {})
        buckets[key][g] = buckets[key].get(g, 0.0) + f.amount_usd
        totales[g] = totales.get(g, 0.0) + f.amount_usd
        if f.is_estimate:
            estimado += f.amount_usd
        else:
            facturado += f.amount_usd

    # OpenAI filtrado, sumado aparte para el corte por fuente/comportamiento
    if group_by in ("source", "behavior"):
        oa = (db.query(CostDaily)
              .filter(CostDaily.day >= since, CostDaily.day <= until,
                      CostDaily.source == "openai",
                      CostDaily.dim_type == "line_item").all())
        for f in oa:
            if f.amount_usd is None:
                continue
            if of and not any(s in (f.dim_value or "").lower() for s in of):
                continue
            key = _bucket_key(f.day, granularity)
            g = "openai" if group_by == "source" else (f.cost_behavior or "variable")
            buckets.setdefault(key, {})
            buckets[key][g] = buckets[key].get(g, 0.0) + f.amount_usd
            totales[g] = totales.get(g, 0.0) + f.amount_usd
            facturado += f.amount_usd

    total = round(sum(totales.values()), 4)
    serie = [{"bucket": k, "total": round(sum(v.values()), 4),
              "by": {g: round(x, 4) for g, x in sorted(v.items())}}
             for k, v in sorted(buckets.items())]

    return {
        "since": since.isoformat(), "until": until.isoformat(),
        "granularity": granularity, "group_by": group_by,
        "total_usd": total,
        "by_group": {g: round(v, 4) for g, v in
                     sorted(totales.items(), key=lambda kv: -kv[1])},
        "series": serie,
        "invoiced_usd": round(facturado, 4),
        "estimated_usd": round(estimado, 4),
        "estimated_share": round(estimado / total, 4) if total else None,
        "coverage": cov,
        "openai_line_item_filter": of,
    }
