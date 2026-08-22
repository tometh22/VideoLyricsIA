"""Margen por TIPO de video, no por tenant ni por canción.

`cost_attribution.py` responde "en nombre de quién se gastó"; este módulo
responde "en cuál de los 3 productos del contrato de Universal". Son
preguntas distintas porque el contrato de UMG no es un precio único: son
400 "lyric videos"/mes a $6 — con la condición contractual de que la MITAD
tiene que ir con fondo de foto fija en vez de Veo, como palanca de costo —
más 250 Art Tracks/mes a $4 (portada + waveform, sin Veo, sin editor de
letra). Sin este corte, el margen agregado puede estar sano mientras uno
de los tres tipos da pérdida y lo compensa el resto sin que nadie lo vea.

Por qué la clasificación NO sale de un JOIN limpio
---------------------------------------------------
No existe una columna `video_type`. Lo que existe son dos booleanos en
`Job.render_params` (JSONB), escritos por caminos de código separados:

* `render_params["art_track"]` — lo setea `main.py` ANTES de encolar el
  pipeline cuando el request trae `art_track=true` (belt-and-suspenders:
  también lo persiste el worker por si muere antes). Es explícito y no
  requiere inferencia.
* `render_params["background_ai_generated"]` — lo escribe
  `pipeline._background_source_is_ai()` (ver `pipeline.py`) durante el
  render. `True` = fondo Veo/animado: Static (`False`) = biblioteca, upload
  del operador o Ken Burns sin animar.

Un job puede tener `art_track=True` Y `background_ai_generated=False` al
mismo tiempo (el Art Track usa una imagen de portada estática) — por eso
`art_track` se chequea PRIMERO en `classify_video_type`: es la distinción
de PRODUCTO (¿tiene letra sincronizada o no?), mientras que
`background_ai_generated` es la distinción de COSTO dentro de "lyric
video". Invertir el orden clasificaría todo Art Track como lyric_static y
lo escondería dentro del producto equivocado.

Jobs viejos (pre-instrumentación) no tienen `background_ai_generated` en
absoluto — el campo se empezó a persistir recién cuando se escribió
`_background_source_is_ai`. Existe una reconstrucción legacy para esos
jobs (`pipeline._legacy_background_source_is_ai`, usada por el propio
endpoint de edición), pero ese camino necesita el último `AssetUsage` del
job y fue diseñado para UN job a la vez, no para agregar un mes entero.
Forzar esos jobs a "static" o a "veo" fabricaría una categoría con
plata que en realidad no se puede atribuir. Se cuentan aparte, como
`unknown`, y el endpoint expone cuántos son para que quien lea el reporte
sepa cuánta cobertura tiene.

Por qué la mano de obra puede salir en null para Art Track
------------------------------------------------------------
El "tiempo activo de edición" no se reconstruye acá: ya viene calculado
por el cliente (`readActiveEditMs()` en `LyricsEditor.jsx`) y viaja en
`ProductEvent(name="editor_approved").properties["active_edit_ms"]`. Ese
evento sólo lo emite el editor de LETRA. El flujo de Art Track nunca abre
ese editor — `App.jsx` lo dice explícito: "background_file + art_track=true
+ empty segments. No lyrics editor, no R2" — así que un Art Track jamás
genera un `editor_approved`. No es un bug de este módulo ni un hueco de
telemetría: es que ese producto estructuralmente no tiene esa mano de obra
que medir. `labor_minutes_avg`/`labor_minutes_p50` salen `None` para
`art_track` salvo que la muestra exista (por ejemplo, un Art Track que se
retocó a mano después vía el editor). Tratar `None` como `0` inflaría el
margen del producto que menos se edita.

Ventana de la muestra de mano de obra
--------------------------------------
La duración se asocia por `job_id`, sin acotar `ProductEvent.created_at`
al período. La edición de un job casi siempre ocurre pegada a su entrega,
pero cuando no es así (una re-edición semanas después) igual queremos ese
dato asociado al video que se editó, no perderlo por caer fuera de la
ventana de facturación. El costo de infra, en cambio, SÍ se acota al
período (igual que `cost_attribution.collect_jobs`) porque ahí sí importa
que el gasto cuadre contra la factura del mes.

Qué es "entregado" acá
------------------------
Mismo criterio que el resto del sistema: `delivered_job_filter()` /
`DELIVERED_STATUSES` de `provenance.py`. No se reinventa qué cuenta como
entregado — solo se le agrega el corte por tipo de producto encima.

Sesgo conocido: staging vs prod
---------------------------------
Este módulo lee UNA sola sesión de DB (la que le pase el caller), igual
que `/admin/cost` y `/admin/margin`. A diferencia de `/admin/cost/umg`,
NO hace merge cross-entorno — la producción gestionada de UMG corre en
staging bajo cuentas del equipo (ver `cost_attribution.py`), así que un
reporte corrido contra prod puede subestimar fuerte el volumen real de
lyric videos entregados. El caller que necesite la vista completa de
negocio debe correr esto contra cada entorno y sumar, igual que hace
`scripts/umg_cost_report.py` para la atribución por canción.
"""
from __future__ import annotations

import statistics

from sqlalchemy import func

from database import AIProvenance, Job, ProductEvent
from provenance import billable_filter, cost_for_record, delivered_job_filter

CAT_LYRIC_VEO = "lyric_veo"
CAT_LYRIC_STATIC = "lyric_static"
CAT_ART_TRACK = "art_track"
CAT_UNKNOWN = "unknown"

# The three billable product categories. `CAT_UNKNOWN` is tracked
# separately and never mixed into these — see module docstring.
VIDEO_TYPE_CATEGORIES = (CAT_LYRIC_VEO, CAT_LYRIC_STATIC, CAT_ART_TRACK)

# Same assumption already used elsewhere when a labor cost has to be
# estimated without a real payroll figure (no other rate is documented in
# the repo as of 2026-08). Overridable per-request via `labor_rate_usd_per_hour`.
DEFAULT_LABOR_RATE_USD_PER_HOUR = 10.0


def classify_video_type(render_params: dict | None) -> str:
    """Bucket a job's `render_params` into one of the 3 product categories.

    Order matters: `art_track` is checked FIRST because it is the product
    distinction (does this video have synced lyrics at all?), independent
    of what its (static) background looks like. See module docstring.
    """
    params = render_params or {}
    if params.get("art_track") is True:
        return CAT_ART_TRACK
    ai_generated = params.get("background_ai_generated")
    if ai_generated is True:
        return CAT_LYRIC_VEO
    if ai_generated is False:
        return CAT_LYRIC_STATIC
    # Missing/non-bool: pre-instrumentation job or a render path that never
    # persisted the bit. Never guessed into a billable category.
    return CAT_UNKNOWN


def _percentile(values: list[float], quantile: float) -> float | None:
    """Same semantics as the one inlined in `main.product_metrics`."""
    if not values:
        return None
    ordered = sorted(values)
    if quantile == 0.5:
        return statistics.median(ordered)
    import math
    index = max(0, min(len(ordered) - 1, math.ceil(quantile * len(ordered)) - 1))
    return ordered[index]


def collect_delivered_job_ids_by_type(db, start, end) -> dict[str, list[str]]:
    """Delivered job ids in [start, end), grouped by product category.

    "Delivered" mirrors `delivered_job_filter()` — current terminal status
    OR a retained-delivery timestamp pattern for a job now mid-edit. Bucketed
    by `coalesce(completed_at, created_at)`, same as `cost_waste_breakdown`:
    a song counts in the month it actually shipped, not the month it was
    started.
    """
    delivered_at = func.coalesce(Job.completed_at, Job.created_at)
    rows = (
        db.query(Job.job_id, Job.render_params)
        .filter(delivered_job_filter())
        .filter(delivered_at >= start, delivered_at < end)
        .all()
    )
    by_type: dict[str, list[str]] = {c: [] for c in VIDEO_TYPE_CATEGORIES}
    by_type[CAT_UNKNOWN] = []
    for job_id, render_params in rows:
        by_type[classify_video_type(render_params)].append(job_id)
    return by_type


def infra_cost_by_job(db, job_ids: list[str], start, end,
                      rates: dict[str, float] | None = None) -> dict[str, float]:
    """Billable AI spend per job, bounded to [start, end).

    Uses the same `billable_filter()` as every other cost surface in the
    codebase (excludes cache hits, budget-rejected/pending/released rows —
    see `provenance.py`), so a video that reused a cached Veo clip is not
    double-billed here relative to `/admin/cost` or `/admin/margin`.
    """
    if not job_ids:
        return {}
    rows = (
        db.query(AIProvenance.job_id, AIProvenance.tool_name,
                 AIProvenance.tool_provider, func.count(AIProvenance.id))
        .filter(AIProvenance.job_id.in_(job_ids))
        .filter(billable_filter())
        .filter(AIProvenance.created_at >= start, AIProvenance.created_at < end)
        .group_by(AIProvenance.job_id, AIProvenance.tool_name,
                  AIProvenance.tool_provider)
        .all()
    )
    cost_by_job: dict[str, float] = {}
    for job_id, tool_name, tool_provider, calls in rows:
        cost = calls * cost_for_record(tool_name, tool_provider, rates)
        cost_by_job[job_id] = cost_by_job.get(job_id, 0.0) + cost
    return cost_by_job


def labor_ms_by_job(db, job_ids: list[str]) -> dict[str, float]:
    """Active lyric-editing time per job, in milliseconds.

    Reads `editor_approved.properties.active_edit_ms` — the same field
    `main.product_metrics` uses for `operator_review.p50_ms` — rather than
    re-deriving it from `editor_activity_heartbeat` rows. `active_edit_ms`
    is computed client-side by `readActiveEditMs()` (LyricsEditor.jsx) and
    already excludes idle time (>60s since the last interaction stops
    accruing), so re-aggregating heartbeats here would be reinventing a
    number the client already sends and risks disagreeing with the panel
    that already reports it.

    A job can be approved more than once (re-opened after a change
    request). All of its `active_edit_ms` values are summed — the operator
    genuinely spent that much active time across every review pass of that
    video, which is the number that belongs in a cost-per-video estimate.
    Deduplicates identical (job_id, revision) pairs the way
    `main.product_metrics` does, since a retried `/analytics/events` POST
    can insert the same approval twice.
    """
    if not job_ids:
        return {}
    rows = (
        db.query(ProductEvent.job_id, ProductEvent.properties,
                 ProductEvent.created_at, ProductEvent.id)
        .filter(ProductEvent.name == "editor_approved")
        .filter(ProductEvent.job_id.in_(job_ids))
        .order_by(ProductEvent.created_at.desc(), ProductEvent.id.desc())
        .all()
    )
    seen: set[tuple] = set()
    ms_by_job: dict[str, float] = {}
    for job_id, properties, _created_at, _row_id in rows:
        properties = properties or {}
        dedup_key = (job_id, properties.get("revision"))
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        active_ms = properties.get("active_edit_ms")
        if isinstance(active_ms, (int, float)):
            ms_by_job[job_id] = ms_by_job.get(job_id, 0.0) + float(active_ms)
    return ms_by_job


def _category_report(job_ids: list[str], cost_by_job: dict[str, float],
                     labor_ms_by_job_map: dict[str, float],
                     labor_rate_usd_per_hour: float,
                     price_usd: float | None) -> dict:
    delivered_count = len(job_ids)
    costs = [cost_by_job.get(j, 0.0) for j in job_ids]
    infra_cost_avg_usd = (
        round(sum(costs) / delivered_count, 4) if delivered_count else None
    )

    labor_minutes = [
        labor_ms_by_job_map[j] / 60_000.0
        for j in job_ids if j in labor_ms_by_job_map
    ]
    labor_minutes_p50 = _percentile(labor_minutes, 0.5)
    labor_minutes_avg = (
        round(sum(labor_minutes) / len(labor_minutes), 2)
        if labor_minutes else None
    )
    if labor_minutes_p50 is not None:
        labor_minutes_p50 = round(labor_minutes_p50, 2)

    labor_cost_avg_usd = (
        round((labor_minutes_avg / 60.0) * labor_rate_usd_per_hour, 4)
        if labor_minutes_avg is not None else None
    )

    if infra_cost_avg_usd is None:
        total_cost_avg_usd = None
    else:
        total_cost_avg_usd = round(
            infra_cost_avg_usd + (labor_cost_avg_usd or 0.0), 4,
        )

    report = {
        "delivered_count": delivered_count,
        "infra_cost_avg_usd": infra_cost_avg_usd,
        "labor_sample_size": len(labor_minutes),
        "labor_minutes_p50": labor_minutes_p50,
        "labor_minutes_avg": labor_minutes_avg,
        "labor_cost_avg_usd": labor_cost_avg_usd,
        "total_cost_avg_usd": total_cost_avg_usd,
    }
    if price_usd is not None and total_cost_avg_usd is not None:
        report["price_usd"] = price_usd
        report["margin_usd"] = round(price_usd - total_cost_avg_usd, 4)
        report["margin_pct"] = (
            round((price_usd - total_cost_avg_usd) / price_usd, 4)
            if price_usd else None
        )
    return report


def build_cost_by_video_type(
    db,
    period: str,
    labor_rate_usd_per_hour: float = DEFAULT_LABOR_RATE_USD_PER_HOUR,
    prices_usd: dict[str, float] | None = None,
) -> dict:
    """Full report for one calendar month, one category per key.

    `prices_usd` optionally maps category -> contract price/video; when a
    category has a price, its entry also carries `margin_usd`/`margin_pct`.
    Without a price the endpoint reports cost only — see module docstring
    on why guessing a price is worse than omitting margin.
    """
    from cost_attribution import period_bounds
    from rate_calibration import load_applied_rates

    start, end = period_bounds(period)
    prices_usd = prices_usd or {}

    by_type = collect_delivered_job_ids_by_type(db, start, end)
    all_job_ids = [j for jobs in by_type.values() for j in jobs]

    rates = load_applied_rates(db, period)
    cost_by_job = infra_cost_by_job(db, all_job_ids, start, end, rates=rates)
    labor_ms = labor_ms_by_job(db, all_job_ids)

    categories = {}
    for cat in VIDEO_TYPE_CATEGORIES:
        job_ids = by_type.get(cat, [])
        categories[cat] = _category_report(
            job_ids, cost_by_job, labor_ms, labor_rate_usd_per_hour,
            prices_usd.get(cat),
        )

    unknown_ids = by_type.get(CAT_UNKNOWN, [])
    return {
        "period": period,
        "labor_rate_usd_per_hour": labor_rate_usd_per_hour,
        "categories": categories,
        # Jobs delivered in the period whose render_params never persisted
        # `background_ai_generated` (pre-instrumentation) — never folded
        # into a billable category. See module docstring.
        "unknown": {
            "delivered_count": len(unknown_ids),
        },
        "total_delivered_count": len(all_job_ids),
    }
