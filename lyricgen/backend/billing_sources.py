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
from datetime import date, datetime, timezone
from typing import Any, Callable

import requests

logger = logging.getLogger("genly.billing")

# Providers can be slow; keep the panel responsive.
HTTP_TIMEOUT = int(os.environ.get("BILLING_HTTP_TIMEOUT", "30"))


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
    year, month = (int(x) for x in period.split("-", 1))
    if not 1 <= month <= 12:
        raise ValueError(f"invalid month in period {period!r}")
    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def current_period() -> str:
    now = datetime.now(timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"


def _not_configured(source: str, period: str, missing: str) -> SourceCost:
    return SourceCost(
        source=source, period=period, status="not_configured",
        detail=f"falta configurar: {missing}",
    )


# ---------------------------------------------------------------------------
# Google Cloud — Vertex (Veo/Imagen/Gemini). ~80% of variable spend.
# ---------------------------------------------------------------------------

def fetch_gcp(period: str) -> SourceCost:
    """Monthly Google Cloud cost from the BigQuery billing export.

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

    start, end = _period_bounds(period)
    # cost + credits is the number that actually lands on the invoice;
    # `cost` alone ignores sustained-use discounts and promo credits.
    sql = f"""
        SELECT
          service.description AS service,
          SUM(cost) AS cost,
          SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)) AS credits
        FROM `{project}.{dataset}.{table}`
        WHERE DATE(usage_start_time) BETWEEN '{start.isoformat()}' AND '{end.isoformat()}'
        GROUP BY service
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

    breakdown = []
    total = 0.0
    for row in payload.get("rows", []) or []:
        cells = row.get("f", [])
        if len(cells) < 3:
            continue
        service = cells[0].get("v") or "(sin nombre)"
        cost = float(cells[1].get("v") or 0.0)
        credits = float(cells[2].get("v") or 0.0)
        net = cost + credits          # credits arrive negative
        total += net
        breakdown.append({"service": service, "cost": round(net, 4)})

    return SourceCost(
        source="gcp", period=period, amount_usd=round(total, 2),
        breakdown=breakdown,
        raw={"rows": payload.get("totalRows"), "table": f"{dataset}.{table}"},
    )


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

def fetch_railway(period: str) -> SourceCost:
    """Railway usage for the month via the public GraphQL API.

    Env:
        RAILWAY_API_TOKEN       account or team token
        RAILWAY_WORKSPACE_ID    (optional) restrict to one workspace
        RAILWAY_PROJECT_ID      (optional) restrict to the Genly IA project

    Railway bills usage per resource (CPU/RAM/network/volume) and reports
    it in *micro-dollars* under `estimatedUsage`. Scoping to the Genly
    project matters: the account also carries unrelated projects, and the
    jun-2026 numbers differed ($124.54 Genly vs $135.25 whole account).
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
      estimatedUsage(startDate: $startDate, endDate: $endDate,
                     projectId: $projectId, workspaceId: $workspaceId) {
        measurement
        estimatedValue
      }
    }
    """

    variables = {
        "startDate": f"{start.isoformat()}T00:00:00Z",
        "endDate": f"{end.isoformat()}T23:59:59Z",
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

    items = (payload.get("data") or {}).get("estimatedUsage") or []
    breakdown = []
    total = 0.0
    for item in items:
        # estimatedValue is in USD for cost measurements; Railway also
        # returns non-cost measurements (CPU seconds, GB) which we skip.
        measurement = item.get("measurement", "")
        value = float(item.get("estimatedValue") or 0.0)
        if "COST" not in measurement.upper() and value and value > 1000:
            continue  # a raw usage metric, not dollars
        total += value
        breakdown.append({"measurement": measurement, "cost": round(value, 4)})

    return SourceCost(
        source="railway", period=period, amount_usd=round(total, 2),
        breakdown=breakdown, is_estimate=True,
        detail="Railway reporta uso estimado en vivo; cierra a fin de mes.",
        raw={"scope": project_id or workspace_id or "cuenta completa"},
    )


# ---------------------------------------------------------------------------
# OpenAI — whisper-1 transcription.
# ---------------------------------------------------------------------------

def fetch_openai(period: str) -> SourceCost:
    """OpenAI spend from the organization Costs API.

    Env:
        OPENAI_ADMIN_KEY   an *admin* key (sk-admin-...), NOT the regular
                           API key — the regular key cannot read billing.

    Returns daily buckets summed for the month.
    """
    key = os.environ.get("OPENAI_ADMIN_KEY", "").strip()
    if not key:
        return _not_configured("openai", period, "OPENAI_ADMIN_KEY")

    start, end = _period_bounds(period)
    start_ts = int(datetime(start.year, start.month, start.day,
                            tzinfo=timezone.utc).timestamp())
    end_ts = int(datetime(end.year, end.month, end.day, 23, 59, 59,
                          tzinfo=timezone.utc).timestamp())

    total = 0.0
    breakdown: dict[str, float] = {}
    page = None
    try:
        # The endpoint paginates a day at a time; 31 days fits in a couple
        # of pages but loop defensively with a hard cap.
        for _ in range(20):
            params = {"start_time": start_ts, "end_time": end_ts, "limit": 180}
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
                    amount = (result.get("amount") or {}).get("value") or 0.0
                    line = result.get("line_item") or "otros"
                    total += float(amount)
                    breakdown[line] = breakdown.get(line, 0.0) + float(amount)
            if not payload.get("has_more"):
                break
            page = payload.get("next_page")
            if not page:
                break
    except Exception as e:
        return SourceCost("openai", period, status="error", detail=str(e))

    return SourceCost(
        source="openai", period=period, amount_usd=round(total, 2),
        breakdown=[{"line_item": k, "cost": round(v, 4)}
                   for k, v in sorted(breakdown.items(),
                                      key=lambda kv: -kv[1])],
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
    # Average the daily maxima to approximate GB-month.
    daily_gb = [
        float((r.get("max") or {}).get("payloadSize") or 0) / (1024 ** 3)
        for r in storage_rows
    ]
    avg_gb = sum(daily_gb) / len(daily_gb) if daily_gb else 0.0
    billable_gb = max(0.0, avg_gb - R2_FREE_GB)
    storage_cost = billable_gb * R2_USD_PER_GB_MONTH

    class_a = class_b = 0
    for row in acct.get("r2OperationsAdaptiveGroups") or []:
        action = ((row.get("dimensions") or {}).get("actionType") or "").lower()
        requests_n = int((row.get("sum") or {}).get("requests") or 0)
        # Cloudflare labels the op; writes/lists are class A, reads class B.
        if any(k in action for k in ("put", "post", "copy", "list", "delete", "multipart")):
            class_a += requests_n
        else:
            class_b += requests_n
    ops_cost = (class_a / 1e6) * R2_USD_PER_MILLION_CLASS_A + \
               (class_b / 1e6) * R2_USD_PER_MILLION_CLASS_B

    objects = max(
        [int((r.get("max") or {}).get("objectCount") or 0) for r in storage_rows]
        or [0]
    )
    return SourceCost(
        source="r2", period=period,
        amount_usd=round(storage_cost + ops_cost, 2), is_estimate=True,
        detail=(f"calculado: {avg_gb:.1f} GB promedio ({billable_gb:.1f} GB "
                f"facturables tras {R2_FREE_GB:.0f} GB gratis), {objects:,} objetos"),
        breakdown=[
            {"concepto": "storage", "cost": round(storage_cost, 4),
             "avg_gb": round(avg_gb, 2)},
            {"concepto": "operaciones clase A", "cost": round(
                (class_a / 1e6) * R2_USD_PER_MILLION_CLASS_A, 4), "requests": class_a},
            {"concepto": "operaciones clase B", "cost": round(
                (class_b / 1e6) * R2_USD_PER_MILLION_CLASS_B, 4), "requests": class_b},
        ],
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
    try:
        for _ in range(50):          # hard page cap
            resp = requests.get(
                url, headers={"Authorization": f"Bearer {token}"},
                timeout=HTTP_TIMEOUT,
            )
            resp.raise_for_status()
            payload = resp.json()
            stop = False
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
    """GitHub Actions minutes billed for the account.

    Env:
        GITHUB_BILLING_TOKEN   PAT with `read:org` + `manage_billing:*`
        GITHUB_BILLING_ORG     org name, or
        GITHUB_BILLING_USER    user login (personal accounts)

    Note the API reports the *current billing cycle*, not an arbitrary
    month, so `period` is echoed back but does not filter.
    """
    token = os.environ.get("GITHUB_BILLING_TOKEN", "").strip()
    org = os.environ.get("GITHUB_BILLING_ORG", "").strip()
    user = os.environ.get("GITHUB_BILLING_USER", "").strip()
    if not token or not (org or user):
        return _not_configured(
            "github", period,
            "GITHUB_BILLING_TOKEN + GITHUB_BILLING_ORG o GITHUB_BILLING_USER")

    scope = f"orgs/{org}" if org else f"users/{user}"
    try:
        resp = requests.get(
            f"https://api.github.com/{scope}/settings/billing/actions",
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json"},
            timeout=HTTP_TIMEOUT,
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
    return SourceCost(
        source="github", period=period, amount_usd=round(paid * rate, 2),
        detail=(f"{used} min usados de {included} incluidos; "
                f"{paid} min facturables — el ciclo es el de facturación, "
                f"no el mes calendario"),
        breakdown=[{"included_minutes": included, "used_minutes": used,
                    "paid_minutes": paid}],
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
    "railway_plan": 20.00,
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
    results: list[dict] = []
    total = 0.0
    ok, missing, errored = [], [], []
    for name in names:
        fn = SOURCES.get(name)
        if fn is None:
            results.append(SourceCost(name, period, status="error",
                                      detail="fuente desconocida").as_dict())
            errored.append(name)
            continue
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

    return {
        "period": period,
        "total_usd": round(total, 2),
        "sources": results,
        "configured": ok,
        "not_configured": missing,
        "errored": errored,
        "complete": not missing and not errored,
    }
