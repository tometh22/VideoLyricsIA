"""Daily cost collection — one row per (day, source, dimension).

`billing_sources.py` answers "what did provider X bill this MONTH". This
module answers the same question per DAY, which is what the panel needs to
show a trend, a week, or the effect of a change three days after shipping it.

THE RULE THAT MAKES THIS CORRECT
--------------------------------
**The collector stores the provider's raw granularity. Every business rule
is applied at read time.**

That is not a style preference, it is the fix for a whole class of bugs
found while designing this:

* R2's free allowances (10 GB-month, 1M class-A, 10M class-B) are functions
  of the MONTH. Subtracting 1M class-A per day yields $0 every single day
  even when the month sails past the million; subtracting 1/31 of a million
  per day is also wrong, because the excess is not linear.
* Railway's plan minimum is `max(metered, $20)` over the month. No per-day
  split reproduces a floor.
* OpenAI's line-item filter is a moving target — July's `gpt-4o-mini` spend
  was NOT ours, August's is. Filtering at collect time freezes a wrong
  answer into history, and the collector only looks back 35 days.

WHAT ALREADY WENT WRONG (kept here so it does not come back)
------------------------------------------------------------
`billing_sources.fetch_railway` converts unit-minutes to unit-MONTHS by
dividing by the length of the requested window. Call it with a one-day
window and 8 GB of resident memory becomes "8 GB-months" = $80 for one day.
Measured against the real project: summing seven one-day calls gives
$613.51 where the whole month is $101.14 — **26.9x**. `_railway_day` below
divides by the minutes of the MONTH the day belongs to, always.

Writes are DELETE-then-INSERT per (day, source, grain) in one transaction,
never upsert. A dimension that stops being reported — a SKU reversed into a
credit, a tenant that went quiet — has to disappear, otherwise SUM(dims)
drifts permanently above the total row and nobody can tell why.
"""

from __future__ import annotations

import calendar
import logging
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.orm import Session

import billing_sources as bs
from database import CostCollectionRun, CostDaily

logger = logging.getLogger("genly.costs.daily")

# How far back a gap-driven backfill will reach. Beyond this, provider
# rolling windows have aged out anyway (Replicate paginates predictions that
# eventually disappear; Railway only exposes the open cycle), so pretending
# we could still recover is worse than admitting we cannot.
BACKFILL_DAYS = int(os.environ.get("COST_BACKFILL_DAYS", "35"))

# Sources this module can collect per-day. `github` is deliberately absent:
# it bills $0 (the included discount covers the metered usage) and its API
# reports a billing CYCLE, not calendar days, so a daily row would be a
# fabrication. `fixed` is monthly by nature — see `_fixed_month`.
DAILY_SOURCES = ("gcp", "railway", "r2", "openai", "replicate")

# POR QUÉ RAILWAY NO TRAE SU LÍNEA "AGENT USAGE" ACÁ
# --------------------------------------------------
# La factura de Railway tiene seis líneas; `_railway_day` modela cinco.
# La sexta (Agent Usage, US$5,07 del ciclo 20-jul→20-ago-2026) sale de la
# query `agentUsage`, que es un CONTADOR ACUMULADO DEL CICLO ABIERTO: no
# acepta rango de fechas, no desglosa por servicio y no se puede volver a
# pedir una vez que el ciclo cerró. Los detalles y la evidencia están en
# `billing_sources`, arriba de `railway_agent_usage`.
#
# Se podría fingir una serie diaria restando el contador de ayer contra el
# de hoy. Sería mentira en tres formas distintas, todas del tipo que este
# archivo existe para evitar:
#   * el backfill de 35 días no puede reconstruir nada — escribiría el
#     contador de HOY sobre 35 días del pasado;
#   * un día que el colector no corrió no se recupera nunca, y su consumo
#     se le carga entero al primer día que sí corre;
#   * no hay `serviceId`, así que la dimensión `service` necesitaría un
#     servicio inventado o dejaría de sumar el total (ver
#     `_check_dims_sum_to_total`).
# El número se captura una vez por mes en el snapshot de `fetch_railway`,
# marcado como excluido del total. Es la única granularidad que existe.


@dataclass
class DayResult:
    """Outcome of collecting one (day, source)."""
    source: str
    day: date
    status: str                     # ok | error | not_configured
    rows: list[dict]
    detail: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row(dim_type: str, dim_value: str, amount: float | None, *,
         qty: float | None = None, unit: str | None = None,
         behavior: str | None = None, basis: str = "measured",
         detail: str | None = None, estimate: bool = False,
         grain: str = "day") -> dict:
    return {
        "grain": grain, "dim_type": dim_type, "dim_value": dim_value,
        "qty": qty, "unit": unit,
        "amount_usd": None if amount is None else round(amount, 6),
        "cost_behavior": behavior, "basis": basis, "basis_detail": detail,
        "is_estimate": estimate,
    }


def _check_dims_sum_to_total(rows: list[dict], source: str, day: date) -> None:
    """Guard the invariant the whole panel rests on.

    Every dimension breakdown must add up to the `total` row. If it does not,
    a query that groups by SKU and one that reads the total disagree, and
    there is no way for a reader to know which is right. Fail loudly at
    collect time instead of shipping an inconsistency into the panel.
    """
    total = next((r["amount_usd"] for r in rows
                  if r["dim_type"] == "total" and r["amount_usd"] is not None), None)
    if total is None:
        return
    by_type: dict[str, float] = {}
    for r in rows:
        if r["dim_type"] == "total" or r["amount_usd"] is None:
            continue
        by_type[r["dim_type"]] = by_type.get(r["dim_type"], 0.0) + r["amount_usd"]
    for dim_type, sub in by_type.items():
        if abs(sub - total) > 0.01:
            raise ValueError(
                f"{source} {day}: dim_type={dim_type} suma {sub:.4f} pero el total "
                f"es {total:.4f} (delta {sub - total:+.4f})"
            )


# ---------------------------------------------------------------------------
# Per-source daily collectors
# ---------------------------------------------------------------------------

_RAILWAY_SERVICE_NAMES: dict[str, str] = {}


def _railway_service_names(token: str, project_id: str) -> dict[str, str]:
    """`serviceId` → nombre legible, cacheado por proceso.

    La query de `usage` sólo trae `tags { serviceId }`, un UUID. Guardarlo
    tal cual dejaba la tabla con `bdf24933-a1ab-…: $144,44`, que no responde
    la pregunta para la que existe el desglose: cuánto del "fijo" es `api`
    —residente, no escala con los renders— y cuánto son los workers.
    Verificado contra el proyecto real: los seis UUID del desglose de julio
    resuelven a Worker, ShortWorker, api, Postgres, Redis y Sentinel.

    Si la consulta falla se devuelve `{}` y el llamador cae al UUID: un
    nombre faltante no puede tirar abajo la recolección del gasto.
    """
    import requests

    if _RAILWAY_SERVICE_NAMES:
        return _RAILWAY_SERVICE_NAMES
    query = ("query($id:String!){project(id:$id){services{edges{node"
             "{id name}}}}}")
    try:
        r = requests.post(
            "https://backboard.railway.com/graphql/v2",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            json={"query": query, "variables": {"id": project_id}},
            timeout=bs.HTTP_TIMEOUT)
        r.raise_for_status()
        edges = (((r.json().get("data") or {}).get("project") or {})
                 .get("services") or {}).get("edges") or []
    except Exception:                                          # noqa: BLE001
        return {}
    for e in edges:
        node = e.get("node") or {}
        if node.get("id") and node.get("name"):
            _RAILWAY_SERVICE_NAMES[node["id"]] = node["name"]
    return _RAILWAY_SERVICE_NAMES


def _railway_day(day: date) -> DayResult:
    """Railway usage for one day, priced against the MONTH's minutes.

    Railway exposes no billed figure over its API, only resource metrics, so
    this is `is_estimate=True` by construction. The plan minimum is NOT
    applied here — it is a monthly floor and belongs to the rollup.
    """
    import json
    import requests

    token = os.environ.get("RAILWAY_API_TOKEN", "").strip()
    if not token:
        return DayResult("railway", day, "not_configured", [],
                         "falta RAILWAY_API_TOKEN")
    project_id = os.environ.get("RAILWAY_PROJECT_ID", "").strip()
    workspace_id = os.environ.get("RAILWAY_WORKSPACE_ID", "").strip()
    if not (project_id or workspace_id):
        return DayResult("railway", day, "not_configured", [],
                         "falta RAILWAY_PROJECT_ID o RAILWAY_WORKSPACE_ID")

    query = """
    query usage($s: DateTime!, $e: DateTime!, $p: String, $w: String) {
      usage(startDate:$s, endDate:$e, projectId:$p, workspaceId:$w,
            groupBy:[SERVICE_ID],
            measurements:[CPU_USAGE, MEMORY_USAGE_GB, NETWORK_TX_GB,
                          DISK_USAGE_GB, BACKUP_USAGE_GB]) {
        measurement value tags { serviceId }
      }
    }
    """
    variables = {
        "s": f"{day.isoformat()}T00:00:00Z",
        "e": f"{(day + timedelta(days=1)).isoformat()}T00:00:00Z",
        "p": project_id or None,
        "w": workspace_id or None,
    }
    try:
        resp = requests.post(
            "https://backboard.railway.com/graphql/v2",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            json={"query": query, "variables": variables}, timeout=bs.HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:                                    # noqa: BLE001
        return DayResult("railway", day, "error", [], str(e))
    if payload.get("errors"):
        return DayResult("railway", day, "error", [],
                         json.dumps(payload["errors"])[:400])

    usage = (payload.get("data") or {}).get("usage") or []
    if not usage:
        # An empty window is indistinguishable from a real zero, and Railway
        # never genuinely bills zero for a running project. Treat as error.
        return DayResult("railway", day, "error", [],
                         "la API no devolvió uso para ese día")

    nombres = _railway_service_names(token, project_id) if project_id else {}
    per_service: dict[str, float] = {}
    per_measure: dict[str, tuple[float, float, str]] = {}
    for entry in usage:
        m = entry.get("measurement", "")
        raw = float(entry.get("value") or 0.0)
        svc_id = (entry.get("tags") or {}).get("serviceId") or ""
        # Nombre si se pudo resolver; si no, el UUID crudo antes que perder
        # la fila.
        svc = nombres.get(svc_id) or svc_id or "(sin servicio)"
        if m == "NETWORK_TX_GB":
            cost, units, unit = raw * bs.RAILWAY_USD_PER_EGRESS_GB, raw, "GB"
        else:
            rate = bs.RAILWAY_RATES_PER_UNIT_MINUTE.get(m)
            if rate is None:
                continue
            # La métrica YA viene en unidad-minutos y la tarifa es por
            # unidad-minuto: no hay ninguna división por ventana, así que
            # tampoco hay forma de equivocarla. La versión original dividía
            # por los minutos de la VENTANA (26,9x de más en grano diario);
            # la siguiente por los del MES, que corregía el 26,9x pero
            # dejaba ±3% según el mes tuviera 28, 30 o 31 días.
            units, cost, unit = raw, raw * rate, "unidad-minuto"
        per_service[svc] = per_service.get(svc, 0.0) + cost
        prev = per_measure.get(m, (0.0, 0.0, unit))
        per_measure[m] = (prev[0] + cost, prev[1] + units, unit)

    total = sum(per_service.values())
    rows = [_row("total", "total", total, behavior="fijo", estimate=True,
                 detail="métricas unidad-minuto × tarifas por minuto del "
                        "Bill Breakdown de Railway")]
    # Por MEDICIÓN: memoria/CPU/disco vs egress.
    for m, (cost, units, unit) in per_measure.items():
        # El compute es capacidad residente: se acumula haya o no renders,
        # así que es piso fijo y no costo por video. El egress es la única
        # línea que sigue de verdad al volumen.
        behavior = "variable" if m == "NETWORK_TX_GB" else "fijo"
        rows.append(_row("sku", m, cost, qty=units, unit=unit,
                         behavior=behavior, estimate=True))
    # Por SERVICIO. Esta dimensión se calculaba y se tiraba: `dim_value`
    # guardaba el nombre de la MEDICIÓN, no el del servicio, así que era
    # imposible separar `api` (residente) de los workers (escalan con
    # renders) — justo la pregunta de cuánto del "fijo" es semi-variable.
    for svc, cost in per_service.items():
        rows.append(_row("service", svc[:255], cost, estimate=True))
    _check_dims_sum_to_total(rows, "railway", day)
    return DayResult("railway", day, "ok", rows)


def _openai_day(day: date) -> DayResult:
    """OpenAI cost for one day, keeping EVERY line item.

    Two traps, both already hit in practice:

    1. **La ventana se recorta a buckets COMPLETOS.** El bucket diario va de
       `D 00:00` a `D+1 00:00`; con `end = D 23:59:59` no entra entero y la
       API devuelve **cero buckets**. Verificado contra la organización real
       el 20-ago-2026: `end=23:59:59` → sin buckets ($0); `end=D+1 00:00` →
       un bucket, $0,1138.

       Ese `23:59:59` venía de arreglar el problema opuesto —una ventana de
       varios días devuelve también el bucket que ARRANCA en `end_time`, y
       sumar días duplicaba—, pero se pasó de largo: el colector guardó
       $0,00 todos los días de julio y agosto contra una factura de $19,41.

       La forma correcta no es achicar la ventana sino **quedarse con el
       bucket del día pedido**: se pide hasta `D+1 00:00` y se descarta
       cualquier bucket que no arranque en `D`. Así el no-doble-conteo no
       depende de cómo la API interprete el borde.
    2. The GenLy subtotal is isolated by a substring filter over line items,
       and the org is shared. If OpenAI renames a line (they already went
       `whisper-1` → `gpt-4o-transcribe`), the filter matches nothing and
       returns $0 with status ok. Storing every line and filtering at read
       time makes that impossible AND lets a filter change be applied to
       history retroactively.
    """
    import requests

    key = os.environ.get("OPENAI_ADMIN_KEY", "").strip()
    if not key:
        return DayResult("openai", day, "not_configured", [],
                         "falta OPENAI_ADMIN_KEY (la key sk-proj no lee facturación)")

    inicio_dia = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    inicio_siguiente = inicio_dia + timedelta(days=1)
    start_ts = int(inicio_dia.timestamp())
    end_ts = int(inicio_siguiente.timestamp())
    params = {"start_time": start_ts, "end_time": end_ts,
              "bucket_width": "1d", "limit": 7, "group_by": "line_item"}
    project_id = os.environ.get("OPENAI_PROJECT_ID", "").strip()
    if project_id:
        params["project_ids"] = project_id

    per_line: dict[str, float] = {}
    page = None
    seen: set[str] = set()
    try:
        for _ in range(10):
            if page:
                params["page"] = page
            r = requests.get("https://api.openai.com/v1/organization/costs",
                             headers={"Authorization": f"Bearer {key}"},
                             params=params, timeout=bs.HTTP_TIMEOUT)
            r.raise_for_status()
            payload = r.json()
            for bucket in payload.get("data", []) or []:
                # Sólo el bucket del día pedido. Una ventana que llega a
                # `D+1 00:00` puede traer también el que arranca ahí.
                inicio = bucket.get("start_time")
                if inicio is not None and not (start_ts <= int(inicio) < end_ts):
                    continue
                for res in bucket.get("results", []) or []:
                    line = res.get("line_item") or "(sin line_item)"
                    amt = float((res.get("amount") or {}).get("value") or 0.0)
                    per_line[line] = per_line.get(line, 0.0) + amt
            if not payload.get("has_more"):
                break
            page = payload.get("next_page")
            if not page or page in seen:
                return DayResult("openai", day, "error", [],
                                 "paginación incompleta: has_more sin cursor nuevo")
            seen.add(page)
    except Exception as e:                                    # noqa: BLE001
        return DayResult("openai", day, "error", [], str(e))

    total = sum(per_line.values())
    rows = [_row("total", "total", total, behavior="variable",
                 detail="org completa; el filtro de line_items se aplica al leer")]
    for line, amt in per_line.items():
        rows.append(_row("line_item", line[:255], amt, behavior="variable"))
    _check_dims_sum_to_total(rows, "openai", day)
    return DayResult("openai", day, "ok", rows)


def _replicate_day(day: date) -> DayResult:
    """Replicate compute for one day, from prediction timestamps.

    Bounded by `< start of next day`, not `<= 23:59:59`: the existing monthly
    fetcher drops predictions that land in the final fraction of a second and
    no other window ever claims them.
    """
    import requests

    token = os.environ.get("REPLICATE_API_TOKEN", "").strip()
    if not token:
        return DayResult("replicate", day, "not_configured", [],
                         "falta REPLICATE_API_TOKEN")

    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    rate = float(os.environ.get("REPLICATE_USD_PER_SECOND", "0.000225"))

    per_model: dict[str, tuple[int, float]] = {}
    url = ("https://api.replicate.com/v1/predictions"
           f"?created_before={end.isoformat().replace('+00:00', 'Z')}")
    pages = 0
    try:
        while url and pages < bs.REPLICATE_MAX_PAGES:
            r = requests.get(url, headers={"Authorization": f"Bearer {token}"},
                             timeout=bs.HTTP_TIMEOUT)
            r.raise_for_status()
            payload = r.json()
            results = payload.get("results") or []
            pages += 1
            if not results:
                break
            oldest = None
            for p in results:
                created = p.get("created_at")
                if not created:
                    continue
                dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                oldest = dt
                if dt >= end or dt < start:
                    continue
                secs = float((p.get("metrics") or {}).get("predict_time") or 0.0)
                model = p.get("model") or p.get("version") or "(sin modelo)"
                n, s = per_model.get(model, (0, 0.0))
                per_model[model] = (n + 1, s + secs)
            if oldest is not None and oldest < start:
                break
            url = payload.get("next")
        else:
            if pages >= bs.REPLICATE_MAX_PAGES:
                return DayResult("replicate", day, "error", [],
                                 f"se agotó el cap de {bs.REPLICATE_MAX_PAGES} páginas")
    except Exception as e:                                    # noqa: BLE001
        return DayResult("replicate", day, "error", [], str(e))

    total = sum(s for _, s in per_model.values()) * rate
    rows = [_row("total", "total", total, behavior="variable", estimate=True,
                 detail=f"segundos de compute × ${rate}/s (tarifa mezclada)")]
    for model, (n, secs) in per_model.items():
        rows.append(_row("sku", model[:255], secs * rate, qty=secs,
                         unit="segundos", behavior="variable", estimate=True,
                         detail=f"{n} predicciones"))
    _check_dims_sum_to_total(rows, "replicate", day)
    return DayResult("replicate", day, "ok", rows)


def _fixed_month(day: date) -> DayResult:
    """Flat subscriptions — stored at grain='month', never split per day.

    Splitting $24/month into $0.77/day is arithmetically fine and
    analytically useless: the number does not respond to anything. Keeping
    it monthly is what lets the reader separate the fixed floor from the
    marginal cost of one more video.
    """
    period = f"{day.year:04d}-{day.month:02d}"
    src = bs.fetch_fixed(period)
    if src.status != "ok":
        return DayResult("fixed", day, src.status, [], src.detail)

    rows = [_row("total", "total", src.amount_usd, behavior="fijo", grain="month",
                 detail="suscripciones planas del mes")]
    for item in src.breakdown or []:
        rows.append(_row("sku", str(item["concepto"])[:255], float(item["cost"]),
                         behavior="fijo", grain="month",
                         detail="suscripción plana, sin API"))
    _check_dims_sum_to_total(rows, "fixed", day)
    return DayResult("fixed", day, "ok", rows)


# Los SKU de Cloud Storage son consecuencia acumulada de entregas pasadas,
# igual que R2: no se mueven con la producción de este mes y no bajan solos.
# Marcarlos 'variable' junto con Vertex haría que el costo marginal de un
# video más se vea más caro de lo que es.
_GCP_STOCK = ("storage", "cloud storage", "egress", "network")


def _gcp_behavior(sku: str) -> str:
    low = (sku or "").lower()
    if any(k in low for k in _GCP_STOCK):
        return "stock"
    return "variable"


def gcp_month(any_day_in_month: date) -> list[DayResult]:
    """GCP for the WHOLE month in ONE query, returned split by day and SKU.

    A batch collector, not a per-day one, and that shape is the point.

    The billing export table is partitioned by INGESTION time
    (`timePartitioning.field` is null), so `WHERE DATE(usage_start_time)=...`
    prunes nothing and scans the entire table. Thirty of those a month,
    against a table that only grows and bills $6.25/TiB, is the cost panel
    generating cost — roughly $28/month at a 5 GB table versus $0 for a
    single grouped query inside the free tier.

    Three guards, each for a failure that would corrupt the numbers rather
    than announce itself:

    * `maximumBytesBilled` — a runaway scan fails loudly instead of billing.
    * `timeoutMs` strictly below the HTTP timeout, so BigQuery's own
      `jobComplete: false` message survives instead of racing the socket.
    * `totalRows` compared against rows actually read: grouped by day the
      result is thousands of rows, and a silent truncation would simply look
      like a cheaper month.

    Credits get their own `__creditos__` row instead of being netted into
    each SKU. A credit posted on one day can push that day negative, and a
    negative day makes cost-per-video meaningless and inverts any
    share-based allocation.
    """
    import requests

    project = os.environ.get("GCP_BILLING_BQ_PROJECT", "").strip()
    dataset = os.environ.get("GCP_BILLING_BQ_DATASET", "").strip()
    table = os.environ.get("GCP_BILLING_BQ_TABLE", "").strip()
    first = any_day_in_month.replace(day=1)
    last = date(first.year, first.month,
                calendar.monthrange(first.year, first.month)[1])

    def _todos(status: str, detail: str) -> list[DayResult]:
        d, out = first, []
        while d <= last:
            out.append(DayResult("gcp", d, status, [], detail))
            d += timedelta(days=1)
        return out

    if not (project and dataset and table):
        return _todos("not_configured",
                      "faltan GCP_BILLING_BQ_PROJECT/DATASET/TABLE")
    proyectos = [p.strip() for p in
                 os.environ.get("GCP_BILLING_PROJECT_IDS", "").split(",") if p.strip()]
    if not proyectos:
        return _todos("not_configured",
                      "falta GCP_BILLING_PROJECT_IDS (el export cubre toda la cuenta)")

    # Si el proyecto de Vertex quedara fuera del scope, GCP devolvería un
    # número más chico con status ok y nadie se enteraría.
    vertex = os.environ.get("VERTEX_PROJECT", "").strip()
    if vertex and vertex not in proyectos:
        return _todos("error",
                      f"VERTEX_PROJECT={vertex} no está en GCP_BILLING_PROJECT_IDS")

    try:
        creds = bs._gcp_credentials()          # noqa: SLF001
    except Exception as e:                                    # noqa: BLE001
        return _todos("error", f"credenciales GCP: {e}")

    # Los registros se exportan días después del uso: el filtro de partición
    # necesita margen o se pierden las restatements tardías.
    part_from, part_to = first - timedelta(days=2), last + timedelta(days=45)
    lista = ", ".join(f"'{p}'" for p in proyectos)
    sql = f"""
        SELECT DATE(usage_start_time, 'UTC') AS dia,
               sku.description AS sku,
               SUM(cost) AS bruto,
               SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)) AS creditos
        FROM `{project}.{dataset}.{table}`
        WHERE _PARTITIONTIME BETWEEN TIMESTAMP('{part_from}') AND TIMESTAMP('{part_to}')
          AND DATE(usage_start_time, 'UTC') BETWEEN '{first}' AND '{last}'
          AND project.id IN ({lista})
        GROUP BY dia, sku
    """
    try:
        resp = requests.post(
            f"https://bigquery.googleapis.com/bigquery/v2/projects/{project}/queries",
            headers={"Authorization": f"Bearer {creds}",
                     "Content-Type": "application/json"},
            json={"query": sql, "useLegacySql": False,
                  "timeoutMs": max(5, bs.HTTP_TIMEOUT - 5) * 1000,
                  "maximumBytesBilled": os.environ.get(
                      "GCP_BILLING_MAX_BYTES", str(20 * 1024 ** 3))},
            timeout=bs.HTTP_TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:                                    # noqa: BLE001
        return _todos("error", str(e))

    if payload.get("jobComplete") is False:
        return _todos("error",
                      "BigQuery no terminó dentro del timeout (jobComplete=false)")
    rows_raw = payload.get("rows") or []
    declared = int(payload.get("totalRows") or 0)
    if declared != len(rows_raw):
        return _todos("error",
                      f"truncado: totalRows={declared}, leídas {len(rows_raw)}")
    if not rows_raw:
        # NO se reporta $0: sería indistinguible de gasto cero. El export no
        # es retroactivo, así que un mes anterior a habilitarlo nunca tendrá
        # filas y eso tiene que verse como falta de dato, no como gratis.
        return _todos("error",
                      "sin filas para el período; el export puede no estar poblado")

    por_dia: dict[date, dict[str, float]] = {}
    creditos: dict[date, float] = {}
    for r in rows_raw:
        cells = r.get("f", [])
        if len(cells) < 4:
            continue
        d = date.fromisoformat(cells[0].get("v"))
        sku = (cells[1].get("v") or "(sin sku)")[:255]
        por_dia.setdefault(d, {})
        por_dia[d][sku] = por_dia[d].get(sku, 0.0) + float(cells[2].get("v") or 0.0)
        creditos[d] = creditos.get(d, 0.0) + float(cells[3].get("v") or 0.0)

    # HASTA DÓNDE LLEGA EL EXPORT, no hasta dónde llega el mes.
    #
    # El export de facturación puede cortarse: el de esta cuenta dejó de
    # escribir el 18-ago-2026 y del 19 en adelante no hay una sola fila,
    # aunque `ai_provenance` registra llamadas a Veo el 31-ago y el 1-sep en
    # los dos entornos. Escribir $0,00 para esos días es afirmar que no se
    # gastó, y es falso.
    #
    # Un día POSTERIOR al último con datos no es un cero: es un día que el
    # export todavía no cubre. Se marca como error para que la cobertura se
    # ponga en rojo, en vez de depender de que alguien lea el detector de
    # fuentes sospechosas.
    #
    # El costo de esto es que si los últimos días de un mes de verdad no
    # tuvieron gasto, se reportan como falta de dato. Es el lado correcto
    # para equivocarse: un piso admitido, no un cero inventado.
    ultimo_con_datos = max(por_dia) if por_dia else None

    out, d = [], first
    while d <= last:
        skus = por_dia.get(d, {})
        cred = creditos.get(d, 0.0)
        if not skus and not cred and ultimo_con_datos and d > ultimo_con_datos:
            out.append(DayResult(
                "gcp", d, "error", [],
                f"el export de facturación no llega a este día "
                f"(último con datos: {ultimo_con_datos.isoformat()})"))
            d += timedelta(days=1)
            continue
        if not skus and not cred:
            # Día sin gasto DENTRO del tramo que el export SÍ cubre: es un
            # cero real, no un hueco. Se escribe como tal.
            out.append(DayResult("gcp", d, "ok", [
                _row("total", "total", 0.0, behavior="variable")]))
            d += timedelta(days=1)
            continue
        rows = [_row("total", "total", sum(skus.values()) + cred,
                     detail="cost + credits, tal como cae en la factura")]
        for sku, bruto in skus.items():
            rows.append(_row("sku", sku, bruto,
                             behavior=_gcp_behavior(sku)))
        if cred:
            rows.append(_row("sku", "__creditos__", cred, behavior="variable",
                             detail="créditos y descuentos (negativos)"))
        _check_dims_sum_to_total(rows, "gcp", d)
        out.append(DayResult("gcp", d, "ok", rows))
        d += timedelta(days=1)
    return out



def r2_month(any_day_in_month: date) -> list[DayResult]:
    """R2 storage and operations per day, from Cloudflare's analytics GraphQL.

    Batch like GCP, but for a different reason: Cloudflare's analytics have a
    retention horizon and return **only the days still inside it, without an
    error**. Asking for the whole month at once lets us compare the dates we
    got against the dates we asked for and mark the rest as missing; asking
    day by day, an aged-out day is indistinguishable from a day that cost
    nothing.

    Free allowances (10 GB-month, 1M class-A, 10M class-B) are NOT applied
    here. They are functions of the month: subtracting 1M class-A per day
    yields $0 every day even when the month sails past the million, and
    subtracting 1/31 of a million per day is also wrong because the excess is
    not linear. The rollup applies them.

    Storage is `cost_behavior='stock'` — it is the accumulated consequence of
    every video ever delivered, not a cost of this month's production. It
    never goes down on its own, and there are no lifecycle rules on the
    bucket, so treating it as variable would badly misprice a long contract.
    """
    import requests

    token = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
    account = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
    first = any_day_in_month.replace(day=1)
    last = date(first.year, first.month,
                calendar.monthrange(first.year, first.month)[1])

    def _todos(status: str, detail: str) -> list[DayResult]:
        d, out = first, []
        while d <= last:
            out.append(DayResult("r2", d, status, [], detail))
            d += timedelta(days=1)
        return out

    if not (token and account):
        return _todos("not_configured",
                      "faltan CLOUDFLARE_API_TOKEN / CLOUDFLARE_ACCOUNT_ID")

    bucket = os.environ.get("R2_BUCKET", "").strip()
    filtro_bucket = f', bucketName: "{bucket}"' if bucket else ""
    query = f"""
    query($acc:String!, $from:Date!, $to:Date!) {{
      viewer {{ accounts(filter: {{accountTag: $acc}}) {{
        storage: r2StorageAdaptiveGroups(limit: 400,
            filter: {{date_geq:$from, date_leq:$to{filtro_bucket}}}) {{
          dimensions {{ date }} max {{ payloadSize objectCount }} }}
        ops: r2OperationsAdaptiveGroups(limit: 10000,
            filter: {{date_geq:$from, date_leq:$to{filtro_bucket}}}) {{
          dimensions {{ date actionType }} sum {{ requests }} }}
      }} }} }}
    """
    try:
        r = requests.post("https://api.cloudflare.com/client/v4/graphql",
                          headers={"Authorization": f"Bearer {token}",
                                   "Content-Type": "application/json"},
                          json={"query": query,
                                "variables": {"acc": account,
                                              "from": first.isoformat(),
                                              "to": last.isoformat()}},
                          timeout=bs.HTTP_TIMEOUT)
        r.raise_for_status()
        payload = r.json()
    except Exception as e:                                    # noqa: BLE001
        return _todos("error", str(e))
    if payload.get("errors"):
        import json as _json
        return _todos("error", _json.dumps(payload["errors"])[:300])

    cuentas = (((payload.get("data") or {}).get("viewer") or {}).get("accounts") or [])
    if not cuentas:
        return _todos("error", "la respuesta no trajo cuentas")
    storage = cuentas[0].get("storage") or []
    ops = cuentas[0].get("ops") or []

    gb_por_dia = {date.fromisoformat(x["dimensions"]["date"]):
                  float(x["max"]["payloadSize"]) / 1e9 for x in storage}
    ops_por_dia: dict[date, dict[str, int]] = {}
    for x in ops:
        d = date.fromisoformat(x["dimensions"]["date"])
        # Cloudflare agrupa por acción concreta; A = mutaciones, B = lecturas.
        accion = x["dimensions"].get("actionType") or ""
        clase = "A" if accion.lower().startswith(("put", "post", "copy", "list",
                                                  "create", "delete")) else "B"
        ops_por_dia.setdefault(d, {"A": 0, "B": 0})
        ops_por_dia[d][clase] += int(x["sum"]["requests"] or 0)

    dias_mes = (last - first).days + 1
    out, d = [], first
    while d <= last:
        if d not in gb_por_dia:
            # Fuera de la ventana de retención de Cloudflare: NO es un cero.
            out.append(DayResult("r2", d, "error", [],
                                 "día fuera de la retención de analytics de Cloudflare"))
            d += timedelta(days=1)
            continue
        gb = gb_por_dia[d]
        # GB-mes prorrateado: el storage de un día es 1/dias_mes de un GB-mes.
        costo_storage = (gb / dias_mes) * bs.R2_USD_PER_GB_MONTH
        o = ops_por_dia.get(d, {"A": 0, "B": 0})
        costo_a = o["A"] / 1_000_000 * bs.R2_USD_PER_MILLION_CLASS_A
        costo_b = o["B"] / 1_000_000 * bs.R2_USD_PER_MILLION_CLASS_B
        rows = [
            _row("total", "total", costo_storage + costo_a + costo_b,
                 estimate=True,
                 detail="sin franquicias: son mensuales y se aplican al agregar"),
            _row("sku", "storage", costo_storage, qty=gb, unit="GB",
                 behavior="stock", estimate=True),
            _row("sku", "class_a", costo_a, qty=o["A"], unit="requests",
                 behavior="variable", estimate=True),
            _row("sku", "class_b", costo_b, qty=o["B"], unit="requests",
                 behavior="variable", estimate=True),
        ]
        _check_dims_sum_to_total(rows, "r2", d)
        out.append(DayResult("r2", d, "ok", rows))
        d += timedelta(days=1)
    return out


# Colectores por lote: piden el mes entero en una sola llamada y devuelven
# una fila por día. Existen porque pedir día por día sería o carísimo (GCP
# escanea la tabla entera en cada query) o directamente incorrecto (R2 no
# distingue "fuera de retención" de "no gastaste nada").
def _fixed_month_all_days(any_day: date) -> list[DayResult]:
    """El hecho mensual se guarda UNA vez, pero los 31 días quedan cubiertos.

    Sin esto, `pending_gaps` pide (día, 'fixed') para los 31 días, el
    colector devuelve una sola fila para el día 1, y los otros 30 quedan
    como huecos que se reintentan por siempre y hacen que `complete` nunca
    llegue a verde.
    """
    first = any_day.replace(day=1)
    last = date(first.year, first.month,
                calendar.monthrange(first.year, first.month)[1])
    cabeza = _fixed_month(first)
    out, d = [], first
    while d <= last:
        # El día 1 lleva las filas; el resto sólo marca la corrida como ok.
        out.append(cabeza if d == first
                   else DayResult("fixed", d, cabeza.status, [], cabeza.detail))
        d += timedelta(days=1)
    return out


BATCH_COLLECTORS = {
    "gcp": gcp_month,
    "r2": r2_month,
    "fixed": _fixed_month_all_days,
}


# Fuentes que se piden día por día. Las que se piden por mes entero están en
# BATCH_COLLECTORS, más arriba.
COLLECTORS = {
    "railway": _railway_day,
    "openai": _openai_day,
    "replicate": _replicate_day,
}

ALL_SOURCES = tuple(COLLECTORS) + tuple(BATCH_COLLECTORS)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _persist(db: Session, result: DayResult) -> None:
    """DELETE-then-INSERT for this (day, source), in one transaction.

    Never upsert. A dimension that stops being reported must vanish: if the
    30-ago SKU set was {Veo, Imagen, Gemini} and Google later reclassifies
    Imagen into a credit, an upsert leaves Imagen's stale amount behind and
    SUM(sku) exceeds the total row forever.
    """
    grains = {r["grain"] for r in result.rows} or {"day"}
    for grain in grains:
        db.query(CostDaily).filter(
            CostDaily.day == result.day,
            CostDaily.source == result.source,
            CostDaily.grain == grain,
        ).delete(synchronize_session=False)

    now = datetime.now(timezone.utc)
    for r in result.rows:
        db.add(CostDaily(day=result.day, source=result.source, fetched_at=now, **r))

    run = db.get(CostCollectionRun, (result.day, result.source))
    if run is None:
        run = CostCollectionRun(day=result.day, source=result.source)
        db.add(run)
    run.status = result.status
    run.attempts = (run.attempts or 0) + 1
    run.last_error = result.detail if result.status != "ok" else None
    run.last_attempt_at = now
    db.commit()


def _mark_pending(db: Session, day: date, source: str) -> None:
    """Record the ATTEMPT before making it.

    A process killed mid-call then leaves a `pending` row instead of no row
    at all, so the difference between "we never asked" and "we asked and it
    blew up" survives the crash.
    """
    run = db.get(CostCollectionRun, (day, source))
    if run is None:
        run = CostCollectionRun(day=day, source=source)
        db.add(run)
    run.status = "pending"
    run.last_attempt_at = datetime.now(timezone.utc)
    db.commit()


# ---------------------------------------------------------------------------
# Gap-driven backfill
# ---------------------------------------------------------------------------

# Ventana en la que un $0,00 todavía puede ser "el proveedor no publicó
# aún" y no "ese día no se gastó". Es la misma que `gcp_month` ya usa como
# margen de restatement.
DIAS_RECHECK_CERO = 45


def pending_gaps(db: Session, *, today: date | None = None,
                 days: int = BACKFILL_DAYS) -> list[tuple[date, str]]:
    """Every (day, source) in the window without a successful collection.

    Gap-driven rather than a fixed "re-collect the last 3 days" window: if
    the collector is down for five days, a fixed window silently loses days
    4 and 5 forever, and every source here has a rolling API window that
    eventually ages out. This self-heals after an outage and normally
    returns an empty set, so it costs nothing to run.
    """
    today = today or datetime.now(timezone.utc).date()
    # Yesterday is the newest complete day; today is still accruing.
    last = today - timedelta(days=1)
    first = last - timedelta(days=days - 1)

    done = {
        (r.day, r.source)
        for r in db.query(CostCollectionRun)
        .filter(CostCollectionRun.day >= first,
                CostCollectionRun.day <= last,
                CostCollectionRun.status.in_(("ok", "not_configured")))
        .all()
    }

    # UN `ok` CON $0,00 NO ES UN DÍA TERMINADO.
    #
    # Los proveedores publican tarde y rellenan hacia atrás. Medido el
    # 1-sep-2026: la corrida de las 01:26 UTC guardó agosto con $0,00 del 2
    # en adelante porque el export de facturación de GCP todavía no los
    # tenía; catorce horas después el MISMO colector devolvía $124 para esos
    # mismos días. Julio, en paralelo, pasó de $74,20 a $138,90 entre dos
    # corridas.
    #
    # Como el backfill es guiado por huecos y esos días quedaron en `ok`,
    # nunca se volvían a pedir: agosto se congelaba mal para siempre. El
    # detector `stale_sources` lo hace VISIBLE; esto lo REPARA.
    #
    # Sólo dentro de la ventana de reformulación: `gcp_month` ya asume 45
    # días de margen para restatements tardías. Más allá de eso un cero es
    # un cero de verdad y volver a pedirlo sería gastar en las APIs para
    # siempre.
    limite_recheck = today - timedelta(days=DIAS_RECHECK_CERO)
    en_cero = {
        (r.day, r.source)
        for r in db.query(CostDaily)
        .filter(CostDaily.day >= max(first, limite_recheck),
                CostDaily.day <= last,
                CostDaily.dim_type == "total",
                CostDaily.amount_usd == 0)
        .all()
    }
    done -= en_cero
    gaps = []
    d = first
    while d <= last:
        for source in ALL_SOURCES:
            if (d, source) not in done:
                gaps.append((d, source))
        d += timedelta(days=1)
    return gaps


def collect_day(db: Session, day: date, source: str) -> DayResult:
    """Collect one (day, source) and persist it. Never raises."""
    fn = COLLECTORS.get(source)
    if fn is None:
        return DayResult(source, day, "error", [], f"fuente desconocida: {source}")
    _mark_pending(db, day, source)
    try:
        result = fn(day)
    except Exception as e:                                    # noqa: BLE001
        logger.exception("[costs] %s %s explotó", source, day)
        result = DayResult(source, day, "error", [], f"{type(e).__name__}: {e}")
    _persist(db, result)
    logger.info("[costs] %s %s -> %s (%d filas)",
                source, day, result.status, len(result.rows))
    return result


def collect_month(db: Session, any_day: date, source: str) -> list[DayResult]:
    """Collect a whole month from a batch source in one provider call."""
    fn = BATCH_COLLECTORS[source]
    first = any_day.replace(day=1)
    last = date(first.year, first.month,
                calendar.monthrange(first.year, first.month)[1])
    d = first
    while d <= last:
        _mark_pending(db, d, source)
        d += timedelta(days=1)
    try:
        results = fn(any_day)
    except Exception as e:                                    # noqa: BLE001
        logger.exception("[costs] %s %s explotó", source, any_day)
        results = []
        d = first
        while d <= last:
            results.append(DayResult(source, d, "error", [],
                                     f"{type(e).__name__}: {e}"))
            d += timedelta(days=1)
    for res in results:
        _persist(db, res)
    logger.info("[costs] %s %s-%02d -> %d días", source, first.year, first.month,
                len(results))
    return results


def run_backfill(db: Session, *, today: date | None = None,
                 days: int = BACKFILL_DAYS, limit: int = 400) -> dict:
    """Fill every gap in the window. Idempotent and safe to run often.

    Batch sources are grouped by month so a 35-day window costs at most two
    provider calls each, not seventy. Without this, GCP alone would scan its
    whole export once per missing day.
    """
    gaps = pending_gaps(db, today=today, days=days)[:limit]
    out = {"attempted": len(gaps), "ok": 0, "error": 0, "not_configured": 0,
           "pending": 0, "errors": []}

    meses_batch: dict[tuple[str, int, int], date] = {}
    por_dia: list[tuple[date, str]] = []
    for day, source in gaps:
        if source in BATCH_COLLECTORS:
            meses_batch.setdefault((source, day.year, day.month), day)
        else:
            por_dia.append((day, source))

    resultados: list[DayResult] = []
    for (source, _y, _m), any_day in sorted(meses_batch.items()):
        resultados.extend(collect_month(db, any_day, source))
        time.sleep(0.2)
    for day, source in por_dia:
        resultados.append(collect_day(db, day, source))
        time.sleep(0.2)

    # Sólo se contabilizan los días que estaban en la lista de huecos: un
    # colector batch devuelve el mes entero, incluidos días ya recolectados.
    pedidos = set(gaps)
    for res in resultados:
        if (res.day, res.source) not in pedidos:
            continue
        out[res.status] = out.get(res.status, 0) + 1
        if res.status == "error":
            out["errors"].append({"day": res.day.isoformat(), "source": res.source,
                                  "detail": (res.detail or "")[:200]})
    return out
