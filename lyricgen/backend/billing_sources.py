"""Real invoiced cost, pulled from each provider's billing API.

Why this exists
---------------
`provenance.py` models what we *think* a video costs by multiplying rows in
`ai_provenance` by a per-call rate table. That model is useful (it attributes
cost per job and per tenant, which no invoice can) but it is an estimate: the
jun-2026 reconciliation was $163 modeled vs **$199.53** actually billed by
Google Cloud. It also cannot see anything that does not flow through
`record_ai_call` — Railway compute, R2 storage, Vercel, GitHub.

This module pulls the *invoiced* number from each provider so the admin panel
can show real monthly cost, and so `/admin/cost/reconcile` can report the
variance between modeled and billed. When the variance drifts, the rate table
is wrong and should be recalibrated.

Design rules
------------
* **Every source is optional.** A source with no credentials returns
  ``status="not_configured"`` instead of raising, so the panel degrades to
  "we have Railway and OpenAI but not GCP yet" rather than 500ing.
* **Never raise into the caller.** Network/auth failures are captured as
  ``status="error"`` with the message. Cost reporting must not take down
  the admin panel.
* **No new dependencies.** Uses `requests` (already required) and
  `google.auth` (already required for Vertex). BigQuery is queried over its
  REST API rather than pulling in `google-cloud-bigquery`.

Credentials are all env vars — see ``docs/COST_TRACKING.md`` for the exact
scopes each token needs and how to mint it.
"""

from __future__ import annotations

import calendar
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

import requests

logger = logging.getLogger("genly.billing")

# Providers can be slow; keep the panel responsive.
HTTP_TIMEOUT = int(os.environ.get("BILLING_HTTP_TIMEOUT", "30"))
REPLICATE_MAX_PAGES = 50


# ---------------------------------------------------------------------------
# Result envelope
# ---------------------------------------------------------------------------

@dataclass
class SourceCost:
    """One provider's cost for one month.

    `amount_usd` is None whenever `status != "ok"` — callers must treat
    "not configured" and "zero dollars" as different things, otherwise a
    missing credential silently reads as free.
    """
    source: str
    period: str                      # "YYYY-MM"
    amount_usd: float | None = None
    status: str = "ok"               # ok | not_configured | error
    detail: str | None = None        # error message or config hint
    is_estimate: bool = False        # True when derived, not invoiced
    breakdown: list[dict] = field(default_factory=list)
    raw: dict[str, Any] | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def _period_bounds(period: str) -> tuple[date, date]:
    """"2026-07" -> (2026-07-01, 2026-07-31). Raises ValueError if malformed."""
    if (len(period) != 7 or period[4] != "-"
            or not period[:4].isdigit() or not period[5:].isdigit()):
        raise ValueError(f"invalid month in period {period!r}; expected YYYY-MM")
    year, month = (int(x) for x in period.split("-", 1))
    if not 1 <= month <= 12:
        raise ValueError(f"invalid month in period {period!r}")
    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def current_period() -> str:
    now = datetime.now(timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"


# A snapshot captured while usage is still accruing is provisional. Sources
# with historical APIs become final only after their usual export lag; the
# next successful refresh at/after this boundary can then be frozen. GitHub
# cannot query a past cycle, but an early GitHub capture can still omit usage
# accrued later in the month. Only fixed subscriptions are final regardless of
# capture time; GitHub needs a capture at the calendar boundary (see below).
SNAPSHOT_FINALIZATION_LAG_DAYS: dict[str, int] = {
    "gcp": 3,
    "railway": 1,
    "openai": 1,
    "replicate": 1,
    "r2": 1,
}
SNAPSHOT_FINAL_FROM_OPEN_MONTH = frozenset({"fixed"})


def snapshot_is_final(period: str, source: str,
                      fetched_at: datetime | None) -> bool:
    """Whether a healthy closed-month capture is safe to freeze.

    This deliberately evaluates the timestamp of the *stored capture*, not
    today's date. Otherwise an in-month provisional row would become final by
    merely waiting, without ever fetching the provider's completed invoice.
    """
    if source in SNAPSHOT_FINAL_FROM_OPEN_MONTH:
        return True
    if fetched_at is None:
        return False
    _start, end = _period_bounds(period)
    captured = fetched_at
    if captured.tzinfo is None:  # SQLite/local drops timezone information.
        captured = captured.replace(tzinfo=timezone.utc)
    if source == "github":
        # The legacy Actions endpoint cannot query the just-closed cycle. A
        # capture made earlier in the month remains only a checkpoint after
        # rollover; accepting it would omit late paid minutes. A final-minute
        # capture is the narrowest honest boundary available from this API.
        boundary = datetime.combine(
            end, datetime.max.time(), tzinfo=timezone.utc,
        ).replace(second=0, microsecond=0)
        next_month = datetime.combine(
            end + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc,
        )
        return boundary <= captured < next_month
    final_after = datetime.combine(
        end + timedelta(days=1 + SNAPSHOT_FINALIZATION_LAG_DAYS.get(source, 1)),
        datetime.min.time(),
        tzinfo=timezone.utc,
    )
    return captured >= final_after


def _not_configured(source: str, period: str, missing: str) -> SourceCost:
    return SourceCost(
        source=source, period=period, status="not_configured",
        detail=f"falta configurar: {missing}",
    )


# ---------------------------------------------------------------------------
# Google Cloud — Vertex (Veo/Imagen/Gemini). ~80% of variable spend.
# ---------------------------------------------------------------------------

def fetch_gcp(period: str) -> SourceCost:
    """Monthly Google Cloud invoice cost from the BigQuery billing export.

    GCP has no API that returns "what did I spend last month" directly; the
    supported path is to enable Cloud Billing export to BigQuery (free,
    daily, per-SKU) and query it. We hit BigQuery's REST `jobs.query`
    endpoint so we don't need the `google-cloud-bigquery` package.

    Env:
        GCP_BILLING_BQ_PROJECT   project holding the export dataset
        GCP_BILLING_BQ_DATASET   dataset name
        GCP_BILLING_BQ_TABLE     e.g. gcp_billing_export_v1_XXXXXX_XXXXXX
        GCP_BILLING_SA_JSON      service-account JSON (inline), or rely on
                                 GOOGLE_APPLICATION_CREDENTIALS

    The service account needs `roles/bigquery.jobUser` on the project and
    `roles/bigquery.dataViewer` on the dataset.

    Returns cost broken down by service (Vertex AI vs Cloud Storage vs
    networking), which is what tells you whether a spend spike was Veo or
    something else entirely.
    """
    project = os.environ.get("GCP_BILLING_BQ_PROJECT", "").strip()
    dataset = os.environ.get("GCP_BILLING_BQ_DATASET", "").strip()
    table = os.environ.get("GCP_BILLING_BQ_TABLE", "").strip()
    if not (project and dataset and table):
        return _not_configured(
            "gcp", period,
            "GCP_BILLING_BQ_PROJECT / GCP_BILLING_BQ_DATASET / GCP_BILLING_BQ_TABLE",
        )

    try:
        creds = _gcp_credentials()
    except Exception as e:
        return SourceCost("gcp", period, status="error",
                          detail=f"credenciales GCP: {e}")

    start, _end = _period_bounds(period)
    invoice_month = f"{start.year:04d}{start.month:02d}"
    # La tabla de export es de la CUENTA DE FACTURACIÓN entera, no del
    # proyecto que la hospeda: si esa cuenta tiene otros proyectos, su gasto
    # se suma acá, infla /cost/real, entra en la calibración de tarifas y
    # termina atribuido a clientes de GenLy. `GCP_BILLING_PROJECT_IDS` acota
    # a los proyectos de trabajo.
    #
    # Without an explicit workload scope there is no honest GenLy total: the
    # dataset-hosting project may be different, while querying the whole
    # billing account silently includes unrelated products and corrupts both
    # unit economics and invoice-derived AI rates. Stay incomplete instead.
    proyectos = [p.strip() for p in
                 os.environ.get("GCP_BILLING_PROJECT_IDS", "").split(",")
                 if p.strip()]
    if not proyectos:
        return _not_configured(
            "gcp", period,
            "GCP_BILLING_PROJECT_IDS (proyectos de workload GenLy; el export "
            "cubre toda la cuenta de facturación)",
        )
    _lista = ", ".join(f"'{p}'" for p in proyectos)
    filtro_proyecto = f"AND project.id IN ({_lista})"
    alcance = f"proyectos {', '.join(proyectos)}"
    # cost + credits is the number that actually lands on the invoice;
    # `cost` alone ignores sustained-use discounts and promo credits.
    sql = f"""
        SELECT
          service.description AS service,
          sku.description AS sku,
          SUM(cost) AS cost,
          SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)) AS credits
        FROM `{project}.{dataset}.{table}`
        WHERE invoice.month = '{invoice_month}'
          {filtro_proyecto}
        GROUP BY service, sku
        ORDER BY cost DESC
    """
    url = f"https://bigquery.googleapis.com/bigquery/v2/projects/{project}/queries"
    try:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {creds}",
                     "Content-Type": "application/json"},
            json={"query": sql, "useLegacySql": False, "timeoutMs": HTTP_TIMEOUT * 1000},
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        return SourceCost("gcp", period, status="error", detail=str(e))

    # A BigQuery query that exceeds `timeoutMs` returns HTTP 200 with
    # `jobComplete: false` and NO rows. Treating that as "$0, ok" would
    # report ~half of all spend as free, with `complete: true` — the exact
    # failure this module is supposed to make impossible. Scanning an
    # unpartitioned billing export is slow enough that this is a routine
    # occurrence, not a corner case.
    if payload.get("jobComplete") is False:
        return SourceCost(
            "gcp", period, status="error",
            detail=("la consulta a BigQuery no terminó dentro de "
                    f"{HTTP_TIMEOUT}s (jobComplete=false). Subí "
                    "BILLING_HTTP_TIMEOUT o particioná el export."),
        )
    rows_returned = payload.get("rows")
    if not rows_returned:
        return SourceCost(
            "gcp", period, status="error",
            detail=("BigQuery no devolvió filas para el período. Puede que "
                    "el export no estuviera habilitado todavía (no es "
                    "retroactivo) o que el nombre de tabla sea otro. NO se "
                    "reporta $0 porque sería indistinguible de gasto cero."),
        )

    # SKU granularity is what makes auto-calibration possible: "Vertex AI"
    # as a service lumps Veo, Imagen and Gemini together, and their unit
    # costs differ by ~50x. Only the SKU line isolates Veo.
    breakdown = []
    total = 0.0
    for row in payload.get("rows", []) or []:
        cells = row.get("f", [])
        if len(cells) < 4:
            continue
        service = cells[0].get("v") or "(sin nombre)"
        sku = cells[1].get("v") or "(sin sku)"
        cost = float(cells[2].get("v") or 0.0)
        credits = float(cells[3].get("v") or 0.0)
        net = cost + credits          # credits arrive negative
        total += net
        breakdown.append({"service": service, "sku": sku,
                          "cost": round(net, 4)})
    breakdown.sort(key=lambda r: -r["cost"])

    return SourceCost(
        source="gcp", period=period, amount_usd=round(total, 2),
        breakdown=breakdown,
        detail=f"invoice.month={invoice_month}; alcance: {alcance}",
        raw={"rows": payload.get("totalRows"), "table": f"{dataset}.{table}",
             "invoice_month": invoice_month,
             "project_scope": proyectos or "billing_account"},
    )


# Substrings that identify each tool's SKU line on the Google Cloud bill.
# Matched case-insensitively against `sku.description`. Used to turn the
# invoice into a real per-call rate — see `rate_calibration.py`.
GCP_SKU_PATTERNS: dict[str, tuple[str, ...]] = {
    "veo": ("veo",),
    "imagen": ("imagen",),
    "gemini": ("gemini",),
}


def gcp_cost_by_tool(source: SourceCost) -> dict[str, float]:
    """Collapse a GCP breakdown into {"veo": usd, "imagen": usd, ...}.

    Anything that matches no pattern lands under "otros" instead of being
    dropped — a SKU we don't recognise is money we still spent, and
    silently losing it would make the derived rates look cheaper than
    they are.
    """
    out: dict[str, float] = {}
    for row in source.breakdown or []:
        sku = (row.get("sku") or "").lower()
        cost = float(row.get("cost") or 0.0)
        for tool, pats in GCP_SKU_PATTERNS.items():
            if any(p in sku for p in pats):
                out[tool] = out.get(tool, 0.0) + cost
                break
        else:
            out["otros"] = out.get("otros", 0.0) + cost
    return {k: round(v, 4) for k, v in out.items()}


def _gcp_credentials() -> str:
    """Return an OAuth access token for BigQuery.

    Prefers inline SA JSON (GCP_BILLING_SA_JSON) so the billing reader can
    be a *different*, read-only identity from the Vertex service account —
    a billing-export reader should not be able to spend money.
    """
    from google.auth.transport.requests import Request as GoogleRequest

    scopes = ["https://www.googleapis.com/auth/bigquery.readonly"]
    inline = os.environ.get("GCP_BILLING_SA_JSON", "").strip()
    if inline:
        from google.oauth2 import service_account
        info = json.loads(inline)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=scopes)
    else:
        import google.auth
        creds, _ = google.auth.default(scopes=scopes)
    creds.refresh(GoogleRequest())
    return creds.token


# ---------------------------------------------------------------------------
# Railway — compute/hosting. Second-largest line.
# ---------------------------------------------------------------------------

# Railway's published Pro-plan rates. The GraphQL API deliberately exposes
# only raw resource metrics (there is no COST measurement in the
# MetricMeasurement enum), so dollars have to be derived here.
#
# Resource metrics come back as unit-MINUTES accumulated over the window
# (e.g. MEMORY_USAGE_GB is GB-minutes), except NETWORK_TX_GB which is a
# flow already expressed in GB. Validated against the jun-2026 invoice:
# this model produced $126.02 against $124.54 actually billed (1.2% off).
RAILWAY_RATES_PER_UNIT_MONTH = {
    "CPU_USAGE": float(os.environ.get("RAILWAY_USD_PER_VCPU_MONTH", "20.0")),
    "MEMORY_USAGE_GB": float(os.environ.get("RAILWAY_USD_PER_GB_MONTH", "10.0")),
    "DISK_USAGE_GB": float(os.environ.get("RAILWAY_USD_PER_DISK_GB_MONTH", "0.15")),
    "EPHEMERAL_DISK_USAGE_GB": 0.0,     # included in the plan
    "BACKUP_USAGE_GB": float(os.environ.get("RAILWAY_USD_PER_BACKUP_GB_MONTH", "0.15")),
}
# Charged per GB transferred, not per GB-month.
RAILWAY_USD_PER_EGRESS_GB = float(os.environ.get("RAILWAY_USD_PER_EGRESS_GB", "0.05"))


def _railway_plan_minimum_usd() -> float:
    """Monthly commitment, including the legacy fixed-subscription config."""
    explicit = os.environ.get("RAILWAY_PLAN_MINIMUM_USD", "").strip()
    if explicit:
        return max(0.0, float(explicit))
    raw_fixed = os.environ.get("FIXED_SUBSCRIPTIONS_JSON", "").strip()
    if raw_fixed:
        try:
            legacy = json.loads(raw_fixed).get("railway_plan")
            if legacy is not None:
                return max(0.0, float(legacy))
        except Exception:
            # fetch_fixed reports malformed JSON explicitly; Railway can still
            # use the documented default instead of failing a second source.
            pass
    return 20.0


def fetch_railway(period: str) -> SourceCost:
    """Railway cost for the month, derived from usage metrics.

    Env:
        RAILWAY_API_TOKEN       account/team token (NOT a project token —
                                project tokens cannot read the usage query)
        RAILWAY_PROJECT_ID      (optional) restrict to the Genly IA project
        RAILWAY_WORKSPACE_ID    (optional) restrict to one workspace
        RAILWAY_PLAN_MINIMUM_USD monthly usage commitment (default 20)

    Scoping to the project matters: the account carries unrelated projects
    and the jun-2026 numbers differed ($124.54 Genly vs $135.25 account).

    Marked `is_estimate` because we price the metrics ourselves — Railway
    exposes no billed figure over the API. Watch `NETWORK_TX_GB`: egress
    doubled between jun and jul 2026 and is the fastest-moving line.
    """
    token = os.environ.get("RAILWAY_API_TOKEN", "").strip()
    if not token:
        return _not_configured("railway", period, "RAILWAY_API_TOKEN")

    start, end = _period_bounds(period)
    project_id = os.environ.get("RAILWAY_PROJECT_ID", "").strip()
    workspace_id = os.environ.get("RAILWAY_WORKSPACE_ID", "").strip()

    query = """
    query usage($startDate: DateTime!, $endDate: DateTime!,
                $projectId: String, $workspaceId: String) {
      usage(startDate: $startDate, endDate: $endDate,
            projectId: $projectId, workspaceId: $workspaceId,
            measurements: [CPU_USAGE, MEMORY_USAGE_GB, NETWORK_TX_GB,
                           DISK_USAGE_GB, BACKUP_USAGE_GB]) {
        measurement
        value
      }
    }
    """
    # `end` is the last day of the month; the API wants an exclusive upper
    # bound at midnight of the following day.
    end_exclusive = end + timedelta(days=1)
    variables = {
        "startDate": f"{start.isoformat()}T00:00:00Z",
        "endDate": f"{end_exclusive.isoformat()}T00:00:00Z",
    }
    if project_id:
        variables["projectId"] = project_id
    if workspace_id:
        variables["workspaceId"] = workspace_id

    try:
        resp = requests.post(
            "https://backboard.railway.app/graphql/v2",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            json={"query": query, "variables": variables},
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        return SourceCost("railway", period, status="error", detail=str(e))

    if payload.get("errors"):
        return SourceCost("railway", period, status="error",
                          detail=json.dumps(payload["errors"])[:500])

    rows = (payload.get("data") or {}).get("usage") or []
    if not rows:
        return SourceCost(
            "railway", period, status="error",
            detail=("la API no devolvió uso para ese período/proyecto; "
                    "no se puede distinguir cero real de ventana histórica vacía"),
            raw={"scope": project_id or workspace_id or "cuenta completa"},
        )

    # Minutes in this specific month — 28/29/30/31 days all differ, and
    # using a fixed 43200 would skew every non-30-day month.
    minutes_in_period = ((end_exclusive - start).days) * 24 * 60

    totals: dict[str, float] = {}
    for row in rows:
        m = row.get("measurement", "")
        totals[m] = totals.get(m, 0.0) + float(row.get("value") or 0.0)

    breakdown = []
    total = 0.0
    for measurement, raw_value in sorted(totals.items()):
        if measurement == "NETWORK_TX_GB":
            cost = raw_value * RAILWAY_USD_PER_EGRESS_GB
            units, unit_label = raw_value, "GB transferidos"
        else:
            rate = RAILWAY_RATES_PER_UNIT_MONTH.get(measurement)
            if rate is None:
                continue
            units = raw_value / minutes_in_period      # unit-minutes → unit-months
            cost = units * rate
            unit_label = "unidad-mes"
        total += cost
        breakdown.append({
            "measurement": measurement,
            "units": round(units, 4),
            "unit": unit_label,
            "cost": round(cost, 4),
        })
    metered_total = total
    plan_minimum = _railway_plan_minimum_usd()
    # The commitment is charged at account/workspace scope. When analytics
    # are narrowed to one project, applying the entire top-up to that project
    # can invent spend if sibling projects already consume the commitment.
    minimum_applies = not project_id
    if minimum_applies and metered_total < plan_minimum:
        top_up = plan_minimum - metered_total
        breakdown.append({
            "measurement": "PLAN_MINIMUM_TOP_UP",
            "units": 1.0,
            "unit": "compromiso mensual",
            "cost": round(top_up, 4),
        })
        total = plan_minimum
    breakdown.sort(key=lambda r: -r["cost"])

    return SourceCost(
        source="railway", period=period, amount_usd=round(total, 2),
        breakdown=breakdown, is_estimate=True,
        detail=(
            "calculado sobre métricas de uso con tarifas publicadas "
            + (
                f"y compromiso mínimo de ${plan_minimum:.2f} "
                if minimum_applies else
                "sin imputar el compromiso account-wide al proyecto "
            )
            + "(Railway no expone el importe facturado por API)"
        ),
        raw={"scope": project_id or workspace_id or "cuenta completa",
             "minutes_in_period": minutes_in_period,
             "metered_usage_usd": round(metered_total, 4),
             "plan_minimum_usd": round(plan_minimum, 2),
             "plan_minimum_applied": minimum_applies},
    )


# ---------------------------------------------------------------------------
# OpenAI — whisper-1 transcription.
# ---------------------------------------------------------------------------

# GenLy's OpenAI dependencies are whisper-1 transcription and the default
# gpt-4o-mini lyric formatter. The org's key is shared with unrelated work,
# so summing the whole organization wildly overstates GenLy: jul-2026 was
# $567.43 org-wide (GPT-5.4, GPT-4.1, image models, batch API — other projects)
# against a much smaller GenLy subtotal.
# Substring match, case-insensitive; set to "" to bill the entire org.
OPENAI_LINE_ITEM_FILTER = [
    s.strip().lower()
    for s in os.environ.get(
        "OPENAI_COST_LINE_ITEMS", "whisper,gpt-4o-mini"
    ).split(",")
    if s.strip()
]


def fetch_openai(period: str) -> SourceCost:
    """OpenAI spend from the organization Costs API, filtered to GenLy.

    Env:
        OPENAI_ADMIN_KEY        an *admin* key (sk-admin-...), NOT the
                                regular API key — that one cannot read
                                billing.
        OPENAI_COST_LINE_ITEMS  comma-separated substrings to keep (default
                                "whisper,gpt-4o-mini"). Empty = whole org.
        OPENAI_PROJECT_ID       (optional) restrict to one project.

    Returns daily buckets summed for the month. `breakdown` always lists
    the excluded lines too, so it stays obvious what was filtered out and
    why the number is smaller than the OpenAI dashboard's headline.
    """
    key = os.environ.get("OPENAI_ADMIN_KEY", "").strip()
    if not key:
        return _not_configured("openai", period, "OPENAI_ADMIN_KEY")

    start, end = _period_bounds(period)
    start_ts = int(datetime(start.year, start.month, start.day,
                            tzinfo=timezone.utc).timestamp())
    end_ts = int(datetime(end.year, end.month, end.day, 23, 59, 59,
                          tzinfo=timezone.utc).timestamp())
    project_id = os.environ.get("OPENAI_PROJECT_ID", "").strip()

    def _keep(line_item: str) -> bool:
        if not OPENAI_LINE_ITEM_FILTER:
            return True
        low = (line_item or "").lower()
        return any(f in low for f in OPENAI_LINE_ITEM_FILTER)

    kept: dict[str, float] = {}
    excluded: dict[str, float] = {}
    page = None
    try:
        # One bucket per day; a 31-day month fits in a single page at
        # limit=180, but loop defensively with a hard cap. `page` is a
        # cursor from `next_page` — re-sending the same one would
        # double-count, so bail the moment it stops advancing.
        seen_pages = set()
        for _ in range(20):
            params = {"start_time": start_ts, "end_time": end_ts,
                      "limit": 180, "group_by": "line_item"}
            if project_id:
                params["project_ids"] = project_id
            if page:
                params["page"] = page
            resp = requests.get(
                "https://api.openai.com/v1/organization/costs",
                headers={"Authorization": f"Bearer {key}"},
                params=params, timeout=HTTP_TIMEOUT,
            )
            resp.raise_for_status()
            payload = resp.json()
            for bucket in payload.get("data", []) or []:
                for result in bucket.get("results", []) or []:
                    amount = float((result.get("amount") or {}).get("value") or 0.0)
                    line = result.get("line_item") or "otros"
                    target = kept if _keep(line) else excluded
                    target[line] = target.get(line, 0.0) + amount
            if not payload.get("has_more"):
                break
            next_page = payload.get("next_page")
            if not next_page or next_page in seen_pages:
                return SourceCost(
                    "openai", period, status="error",
                    detail=("paginación incompleta de OpenAI Costs: "
                            "has_more sin cursor nuevo"),
                )
            page = next_page
            seen_pages.add(next_page)
        else:
            return SourceCost(
                "openai", period, status="error",
                detail="paginación incompleta de OpenAI Costs: más de 20 páginas",
            )
    except Exception as e:
        return SourceCost("openai", period, status="error", detail=str(e))

    total = sum(kept.values())
    excluded_total = sum(excluded.values())
    breakdown = [{"line_item": k, "cost": round(v, 4), "incluido": True}
                 for k, v in sorted(kept.items(), key=lambda kv: -kv[1])]
    breakdown += [{"line_item": k, "cost": round(v, 4), "incluido": False}
                  for k, v in sorted(excluded.items(), key=lambda kv: -kv[1])]

    detail = None
    if OPENAI_LINE_ITEM_FILTER:
        detail = (f"filtrado a {'/'.join(OPENAI_LINE_ITEM_FILTER)}; "
                  f"${excluded_total:,.2f} de la organización quedaron "
                  f"afuera por ser de otros proyectos")
    return SourceCost(
        source="openai", period=period, amount_usd=round(total, 2),
        detail=detail, breakdown=breakdown,
    )


# ---------------------------------------------------------------------------
# Cloudflare R2 — storage. The line nobody has ever measured.
# ---------------------------------------------------------------------------

# R2 published pricing. Storage is charged per GB-month; class A ops are
# writes/lists, class B are reads. Egress to the internet is free, which is
# why R2 was chosen over S3 for serving finished videos.
R2_USD_PER_GB_MONTH = float(os.environ.get("R2_USD_PER_GB_MONTH", "0.015"))
R2_USD_PER_MILLION_CLASS_A = float(os.environ.get("R2_USD_PER_M_CLASS_A", "4.50"))
R2_USD_PER_MILLION_CLASS_B = float(os.environ.get("R2_USD_PER_M_CLASS_B", "0.36"))
R2_FREE_GB = float(os.environ.get("R2_FREE_GB", "10"))
R2_FREE_CLASS_A = int(os.environ.get("R2_FREE_CLASS_A", "1000000"))
R2_FREE_CLASS_B = int(os.environ.get("R2_FREE_CLASS_B", "10000000"))


def fetch_r2(period: str) -> SourceCost:
    """Estimate R2 cost from the Cloudflare GraphQL Analytics API.

    Cloudflare does not expose a per-bucket invoice line, so we read the
    metered usage (mean stored bytes, class A/B operation counts) and price
    it with the published rates above.

    Env:
        CLOUDFLARE_API_TOKEN    token with Account Analytics: Read
        CLOUDFLARE_ACCOUNT_ID   account id
        R2_BUCKET               (optional) restrict to one bucket

    Flagged `is_estimate=True` — it is a computed number, not an invoice.
    Worth watching closely: ProRes masters accumulate and there are no
    lifecycle rules on the bucket yet, so this line only grows.
    """
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
    account = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
    if not (token and account):
        return _not_configured(
            "r2", period, "CLOUDFLARE_API_TOKEN / CLOUDFLARE_ACCOUNT_ID")

    start, end = _period_bounds(period)
    bucket = os.environ.get("R2_BUCKET", "").strip()
    bucket_filter = f', bucketName: "{bucket}"' if bucket else ""

    query = f"""
    query {{
      viewer {{
        accounts(filter: {{accountTag: "{account}"}}) {{
          r2StorageAdaptiveGroups(
            limit: 1000,
            filter: {{date_geq: "{start.isoformat()}", date_leq: "{end.isoformat()}"{bucket_filter}}}
          ) {{ max {{ payloadSize objectCount }} dimensions {{ date }} }}
          r2OperationsAdaptiveGroups(
            limit: 1000,
            filter: {{date_geq: "{start.isoformat()}", date_leq: "{end.isoformat()}"{bucket_filter}}}
          ) {{ sum {{ requests }} dimensions {{ actionType }} }}
        }}
      }}
    }}
    """

    try:
        resp = requests.post(
            "https://api.cloudflare.com/client/v4/graphql",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            json={"query": query}, timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        return SourceCost("r2", period, status="error", detail=str(e))

    if payload.get("errors"):
        return SourceCost("r2", period, status="error",
                          detail=json.dumps(payload["errors"])[:500])

    accounts = ((payload.get("data") or {}).get("viewer") or {}).get("accounts") or []
    if not accounts:
        return SourceCost("r2", period, status="error",
                          detail="la cuenta no devolvió datos de R2")
    acct = accounts[0]

    storage_rows = acct.get("r2StorageAdaptiveGroups") or []
    operation_rows = acct.get("r2OperationsAdaptiveGroups") or []
    if not storage_rows and not operation_rows:
        return SourceCost(
            "r2", period, status="error",
            detail=(
                "Cloudflare no devolvió observaciones de storage ni operaciones; "
                "verificá R2_BUCKET y la ventana antes de aceptar costo $0"
            ),
        )
    # Integrate daily maxima over the *whole requested month*. Cloudflare
    # omits dates with no/future observations, so dividing by the number of
    # returned rows would turn ten observed days into a full GB-month and
    # overstate an open month (or a bucket created mid-month).
    daily_gb = [
        float((r.get("max") or {}).get("payloadSize") or 0) / (1024 ** 3)
        for r in storage_rows
    ]
    days_in_month = (end - start).days + 1
    avg_gb = sum(daily_gb) / days_in_month if daily_gb else 0.0
    # R2 allowances belong to the Cloudflare account, not a bucket. A
    # bucket-scoped query cannot know whether sibling buckets consumed them;
    # charging this bucket's metered usage without the account-wide discount
    # is conservative and avoids freezing an understated "complete" cost.
    allowances_apply = not bucket
    included_gb = R2_FREE_GB if allowances_apply else 0.0
    included_class_a = R2_FREE_CLASS_A if allowances_apply else 0
    included_class_b = R2_FREE_CLASS_B if allowances_apply else 0
    billable_gb = max(0.0, avg_gb - included_gb)
    storage_cost = billable_gb * R2_USD_PER_GB_MONTH

    class_a = class_b = 0
    for row in operation_rows:
        action = ((row.get("dimensions") or {}).get("actionType") or "").lower()
        requests_n = int((row.get("sum") or {}).get("requests") or 0)
        # Cloudflare labels the op; deletes and multipart aborts are free,
        # writes/lists are class A, and reads are class B.
        if "delete" in action or "abortmultipart" in action:
            continue
        if any(k in action for k in ("put", "post", "copy", "list", "multipart")):
            class_a += requests_n
        else:
            class_b += requests_n
    # R2 Standard includes 1M Class A and 10M Class B operations each month.
    # Price only the excess, just like storage above. These defaults mirror
    # Cloudflare's published allowances and remain env-tunable if the plan or
    # pricing changes.
    billable_class_a = max(0, class_a - included_class_a)
    billable_class_b = max(0, class_b - included_class_b)
    class_a_cost = (billable_class_a / 1e6) * R2_USD_PER_MILLION_CLASS_A
    class_b_cost = (billable_class_b / 1e6) * R2_USD_PER_MILLION_CLASS_B
    ops_cost = class_a_cost + class_b_cost

    objects = max(
        [int((r.get("max") or {}).get("objectCount") or 0) for r in storage_rows]
        or [0]
    )
    return SourceCost(
        source="r2", period=period,
        amount_usd=round(storage_cost + ops_cost, 2), is_estimate=True,
        detail=(f"calculado: {avg_gb:.1f} GB promedio ({billable_gb:.1f} GB "
                f"facturables tras {included_gb:.0f} GB imputados de franquicia), "
                f"{objects:,} objetos"
                + ("; franquicias account-wide no aplicadas al bucket"
                   if bucket else "")),
        breakdown=[
            {"concepto": "storage", "cost": round(storage_cost, 4),
             "avg_gb": round(avg_gb, 2), "included_gb": included_gb},
            {"concepto": "operaciones clase A", "cost": round(class_a_cost, 4),
             "requests": class_a, "included_requests": included_class_a,
             "billable_requests": billable_class_a},
            {"concepto": "operaciones clase B", "cost": round(class_b_cost, 4),
             "requests": class_b, "included_requests": included_class_b,
             "billable_requests": billable_class_b},
        ],
        raw={"scope": bucket or "cuenta completa",
             "account_allowances_applied": allowances_apply},
    )


# ---------------------------------------------------------------------------
# Replicate — whisperX / forced-align / demucs.
# ---------------------------------------------------------------------------

def fetch_replicate(period: str) -> SourceCost:
    """Replicate spend, derived from prediction run-times.

    Replicate has no billing endpoint, so we list predictions in the window
    and price each by its hardware run-time. Uses the token the pipeline
    already has (`REPLICATE_API_TOKEN`), so this source usually works with
    zero extra setup.

    Flagged `is_estimate=True`: hardware rates vary per model and we apply
    a single blended rate.
    """
    token = os.environ.get("REPLICATE_API_TOKEN", "").strip()
    if not token:
        return _not_configured("replicate", period, "REPLICATE_API_TOKEN")

    blended_rate = float(os.environ.get("REPLICATE_USD_PER_SECOND", "0.000225"))
    start, end = _period_bounds(period)
    start_dt = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    end_dt = datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=timezone.utc)

    url = "https://api.replicate.com/v1/predictions"
    total_seconds = 0.0
    per_model: dict[str, dict] = {}
    pages_fetched = 0
    stop = False
    try:
        for _ in range(REPLICATE_MAX_PAGES):
            resp = requests.get(
                url, headers={"Authorization": f"Bearer {token}"},
                timeout=HTTP_TIMEOUT,
            )
            resp.raise_for_status()
            payload = resp.json()
            pages_fetched += 1
            for pred in payload.get("results", []) or []:
                created = pred.get("created_at")
                if not created:
                    continue
                created_dt = datetime.fromisoformat(
                    created.replace("Z", "+00:00"))
                if created_dt > end_dt:
                    continue
                if created_dt < start_dt:
                    stop = True       # results are newest-first
                    break
                seconds = float(
                    (pred.get("metrics") or {}).get("predict_time") or 0.0)
                model = pred.get("model") or pred.get("version") or "desconocido"
                total_seconds += seconds
                agg = per_model.setdefault(model, {"runs": 0, "seconds": 0.0})
                agg["runs"] += 1
                agg["seconds"] += seconds
            url = payload.get("next")
            if stop or not url:
                break
    except Exception as e:
        return SourceCost("replicate", period, status="error", detail=str(e))

    # Never label a capped subtotal as a complete monthly bill.  A healthy
    # post-close result may be frozen forever, so a remaining cursor must
    # stay incomplete and preserve any previous snapshot at the caller.
    if url and not stop:
        return SourceCost(
            "replicate",
            period,
            status="error",
            detail=(
                "paginación de Replicate truncada después de "
                f"{pages_fetched} páginas; queda un cursor pendiente"
            ),
            raw={"pages_fetched": pages_fetched, "next": url},
        )

    return SourceCost(
        source="replicate", period=period,
        amount_usd=round(total_seconds * blended_rate, 2), is_estimate=True,
        detail=(f"{total_seconds:.0f}s de compute × ${blended_rate}/s "
                f"(tarifa mezclada)"),
        breakdown=[
            {"model": m, "runs": v["runs"], "seconds": round(v["seconds"], 1),
             "cost": round(v["seconds"] * blended_rate, 4)}
            for m, v in sorted(per_model.items(),
                               key=lambda kv: -kv[1]["seconds"])
        ],
    )


# ---------------------------------------------------------------------------
# GitHub — Actions / Codespaces. Billed $0 so far (covered by the plan).
# ---------------------------------------------------------------------------

def fetch_github(period: str) -> SourceCost:
    """GitHub Actions/Codespaces minutes actually billed.

    Env:
        GITHUB_BILLING_TOKEN   PAT — for a PERSONAL account it needs the
                               `user` scope; `repo`/`admin:org` are not
                               enough and the endpoint 404s without it
                               (a 404 here means missing scope, not a
                               missing account).
        GITHUB_BILLING_ORG     org name, or
        GITHUB_BILLING_USER    user login (personal accounts)

    Low value: as of ago-2026 gross metered usage was fully offset by the
    plan's included allowance ($21.93 gross, $21.93 discount, $0 due), so
    the only real GitHub cost is the flat GitHub Pro subscription, which
    `fetch_fixed` already carries. Wire this up only if usage starts
    exceeding the included minutes.

    The API reports the *current billing cycle*, not an arbitrary month,
    so `period` is echoed back but does not filter.
    """
    token = os.environ.get("GITHUB_BILLING_TOKEN", "").strip()
    org = os.environ.get("GITHUB_BILLING_ORG", "").strip()
    user = os.environ.get("GITHUB_BILLING_USER", "").strip()
    if not token or not (org or user):
        return _not_configured(
            "github", period,
            "GITHUB_BILLING_TOKEN + GITHUB_BILLING_ORG o GITHUB_BILLING_USER")

    # La API sólo devuelve el CICLO DE FACTURACIÓN EN CURSO — no acepta un
    # mes. Sin este guard, pedir `period=2026-06` devolvía los minutos de HOY
    # y `store_rates`/`/cost/refresh` los guardaba como si fueran de junio:
    # un número inventado con apariencia de dato histórico. Preferimos
    # declarar que no se puede antes que archivar algo falso.
    if period != current_period():
        return SourceCost(
            "github", period, status="error",
            detail=("la API de GitHub sólo expone el ciclo de facturación en "
                    f"curso; no se puede consultar {period} en retrospectiva. "
                    "Snapshotear el mes mientras está abierto."),
        )

    scope = f"orgs/{org}" if org else f"users/{user}"
    try:
        resp = requests.get(
            f"https://api.github.com/{scope}/settings/billing/actions",
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json"},
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code == 404:
            return SourceCost(
                "github", period, status="error",
                detail=("404 — al PAT le falta el scope `user` (para cuentas "
                        "personales es el único que habilita facturación). "
                        "Poco importa: el uso medido está cubierto por el "
                        "plan y GitHub Pro ya se cuenta en `fixed`."),
            )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        return SourceCost("github", period, status="error", detail=str(e))

    # GitHub reports minutes, and separately how much of it is chargeable.
    included = payload.get("included_minutes", 0)
    used = payload.get("total_minutes_used", 0)
    paid = payload.get("total_paid_minutes_used", 0)
    # Standard Linux runner rate; only paid minutes cost money.
    rate = float(os.environ.get("GITHUB_USD_PER_MINUTE", "0.008"))
    amount = round(float(paid) * rate, 2)
    breakdown = [{"included_minutes": included, "used_minutes": used,
                  "paid_minutes": paid}]

    # This endpoint is account-cycle-to-date, not calendar-month usage. Even
    # a zero is only the current cycle's zero: with a mid-month boundary it
    # can omit paid minutes from the earlier part of this calendar month.
    # Only a cycle starting on day 1 can be archived as monthly billing data.
    cycle_day_raw = os.environ.get(
        "GITHUB_BILLING_CYCLE_DAY", ""
    ).strip()
    try:
        cycle_day = int(cycle_day_raw)
    except (TypeError, ValueError):
        cycle_day = 0
    if not 1 <= cycle_day <= 28:
        return SourceCost(
            "github", period, status="error",
            detail=(
                f"GitHub reportó ${amount:.2f} para su ciclo corriente, "
                "pero GITHUB_BILLING_CYCLE_DAY no está configurado con "
                "un día válido (1-28); no se atribuye a un mes calendario"
            ),
            breakdown=[{**breakdown[0],
                        "observed_cycle_amount_usd": amount}],
        )
    if cycle_day != 1:
        today = datetime.now(timezone.utc).date()
        if today.day >= cycle_day:
            cycle_start = date(today.year, today.month, cycle_day)
        else:
            previous = today.replace(day=1) - timedelta(days=1)
            cycle_start = date(previous.year, previous.month, cycle_day)
        if cycle_start.month == 12:
            cycle_end = date(cycle_start.year + 1, 1, cycle_day)
        else:
            cycle_end = date(
                cycle_start.year, cycle_start.month + 1, cycle_day,
            )
        return SourceCost(
            "github", period, status="error",
            detail=(
                f"GitHub reportó ${amount:.2f} para el ciclo "
                f"{cycle_start.isoformat()}..{cycle_end.isoformat()}, "
                f"que cruza meses calendario; no se atribuye a {period}"
            ),
            breakdown=[{**breakdown[0],
                        "observed_cycle_amount_usd": amount,
                        "cycle_start": cycle_start.isoformat(),
                        "cycle_end_exclusive": cycle_end.isoformat()}],
        )
    return SourceCost(
        source="github", period=period, amount_usd=amount,
        detail=(f"{used} min usados de {included} incluidos; "
                f"{paid} min facturables — el ciclo es el de facturación, "
                f"no el mes calendario"),
        breakdown=breakdown,
    )


# ---------------------------------------------------------------------------
# Fixed subscriptions — no API worth wiring, they never change.
# ---------------------------------------------------------------------------

# Defaults mirror the real invoices audited 2026-08. Override wholesale
# with FIXED_SUBSCRIPTIONS_JSON (a JSON object of {concept: usd}) when a
# plan changes, so nobody has to ship code to fix a price.
DEFAULT_FIXED_SUBSCRIPTIONS = {
    "vercel_pro": 20.00,
    "github_pro": 4.00,
}


def fetch_fixed(period: str) -> SourceCost:
    """Flat monthly subscriptions. Small, but they are the part of the
    unit cost that *amortizes* — at 65 videos/month they are $0.68/video,
    at 400/month they are $0.11. Leaving them out of the model is what
    makes low-volume months look artificially cheap."""
    raw = os.environ.get("FIXED_SUBSCRIPTIONS_JSON", "").strip()
    items = dict(DEFAULT_FIXED_SUBSCRIPTIONS)
    if raw:
        try:
            items = json.loads(raw)
        except Exception as e:
            return SourceCost("fixed", period, status="error",
                              detail=f"FIXED_SUBSCRIPTIONS_JSON inválido: {e}")
    # Backwards compatibility: deployments may still carry this legacy key.
    # It is a minimum usage commitment, modeled inside fetch_railway as
    # max(metered, minimum), never an additive fixed subscription.
    items.pop("railway_plan", None)
    total = sum(float(v) for v in items.values())
    return SourceCost(
        source="fixed", period=period, amount_usd=round(total, 2),
        detail="suscripciones planas configuradas, no consultadas por API",
        breakdown=[{"concepto": k, "cost": round(float(v), 2)}
                   for k, v in sorted(items.items(), key=lambda kv: -float(kv[1]))],
    )


# ---------------------------------------------------------------------------
# Registry + aggregate
# ---------------------------------------------------------------------------

SOURCES: dict[str, Callable[[str], SourceCost]] = {
    "gcp": fetch_gcp,
    "railway": fetch_railway,
    "openai": fetch_openai,
    "r2": fetch_r2,
    "replicate": fetch_replicate,
    "github": fetch_github,
    "fixed": fetch_fixed,
}


def fetch_all(period: str | None = None,
              only: list[str] | None = None) -> dict:
    """Pull every configured source for `period` (defaults to this month).

    Never raises: a source that blows up is reported as status="error" in
    its own entry. `total_usd` sums only the sources that returned ok, and
    `configured`/`missing` tell the caller how complete that total is —
    a $200 total from 2 of 7 sources is not the same as $200 from 7 of 7.
    """
    period = period or current_period()
    _period_bounds(period)   # validate early, fail loudly on a bad period

    names = only or list(SOURCES)
    unknown = sorted(set(names) - set(SOURCES))
    if unknown:
        raise ValueError(
            "fuentes desconocidas: " + ", ".join(unknown)
            + "; válidas: " + ", ".join(SOURCES)
        )
    results: list[dict] = []
    total = 0.0
    ok, missing, errored = [], [], []
    for name in names:
        fn = SOURCES[name]
        try:
            result = fn(period)
        except Exception as e:                       # defence in depth
            logger.exception("billing source %s failed", name)
            result = SourceCost(name, period, status="error", detail=str(e))
        if result.status == "ok" and result.amount_usd is not None:
            total += result.amount_usd
            ok.append(name)
        elif result.status == "not_configured":
            missing.append(name)
        else:
            errored.append(name)
        results.append(result.as_dict())

    not_requested = [name for name in SOURCES if name not in names]
    return {
        "period": period,
        "total_usd": round(total, 2),
        "sources": results,
        "configured": ok,
        "not_configured": missing,
        "errored": errored,
        "not_requested": not_requested,
        "partial": bool(not_requested),
        "complete": not missing and not errored and not not_requested,
    }
