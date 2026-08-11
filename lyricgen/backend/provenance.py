"""AI Provenance recording — tracks every AI tool invocation for UMG compliance.

Also exposes per-tool cost rates and a tenant cost summary helper for the
admin dashboard. Rates are estimates of marginal API cost per call as of
2026-08, sourced from public pricing pages. The dict is the single source
of truth — update it when pricing changes.

Cost accounting rules (2026-08 audit — see docs/COST_TRACKING.md):

* **Cache hits are not billable.** `_generate_veo_video` opens the
  provenance recorder *before* the R2 cache lookup, so a cache hit still
  writes a full row (marked `cache_hit:` in `response_summary`). Counting
  those rows at $0.80 inflated the dashboard by ~19% in jul-2026 (40 of
  248 Veo rows). Every aggregate here filters them out.
* **The denominator is delivered videos, not jobs created.** Dividing
  spend by created jobs hid the fact that 59% of jul-2026 Veo spend went
  to discarded previews and rejected jobs. `cost_waste_breakdown()`
  attributes every billable call to what it actually produced.
* **Modeled cost is calibrated against real invoices.** These per-call
  rates reconcile to within ~18% of the jun-2026 Google Cloud invoice
  ($163 modeled vs $199.53 billed, the gap being staging + real Imagen /
  Gemini token pricing). `billing_sources.py` pulls the real numbers.
"""

import hashlib
import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, case, extract, func, not_, or_
from sqlalchemy.orm import Session

from database import AIProvenance, Job, SessionLocal

logger = logging.getLogger("genly.provenance")


# ---------------------------------------------------------------------------
# Cost rates — single source of truth for the cost dashboard
# ---------------------------------------------------------------------------

# Per-call estimates in USD. Veo costs assume an 8s clip generation per call
# (the palindrome-loop pattern used by this pipeline). Update when pricing
# changes or when the per-call generation length changes.
COST_PER_CALL: dict[tuple[str, str], float] = {
    # Veo video — Fast (no audio) at $0.10/s × 8s = $0.80 per call.
    # Standard models kept for backwards-compat with existing job rows.
    ("veo-3.1-fast-generate-001", "google_vertex"): 0.80,
    ("veo-3.1-generate-001", "google_vertex"): 3.20,
    ("veo-3.0-fast-generate-001", "google_vertex"): 0.80,
    ("veo-3.0-generate-001", "google_vertex"): 3.20,
    ("veo-2.0-generate-001", "google_vertex"): 4.00,
    # Imagen still images. Imagen 4 is the runtime default
    # (pipeline `_generate_imagen_image`) but was missing from this table
    # until the 2026-08 audit, so every Imagen 4 call fell through to
    # DEFAULT_COST_PER_CALL ($0.01) and was under-counted ~4-6x.
    ("imagen-3.0-generate-001", "google_vertex"): 0.04,
    ("imagen-3.0-fast-generate-001", "google_vertex"): 0.02,
    ("imagen-4.0-generate-001", "google_vertex"): 0.04,
    ("imagen-4.0-fast-generate-001", "google_vertex"): 0.02,
    ("imagen-4.0-ultra-generate-001", "google_vertex"): 0.06,
    # Gemini text/multimodal — averaged across our prompt sizes
    ("gemini-2.5-flash", "google_vertex"): 0.01,
    ("gemini-2.5-flash-lite", "google_vertex"): 0.005,
    ("gemini-2.5-pro", "google_vertex"): 0.05,
    # Content validator. ONE provenance row covers a whole video scan, and
    # the scan is one Gemini vision call per extracted frame (up to
    # `max_frames`, currently 48 at 1 fps). Pricing the row like a single
    # flash call under-counted the validator by ~an order of magnitude;
    # this rate is per-scan, not per-frame.
    ("gemini-2.5-flash-vision", "google_vertex"): 0.02,
    # Single still-image validation performs exactly one frame request. Keep
    # a separate key so it is not priced like the up-to-48-frame video scan.
    ("gemini-2.5-flash-vision-image", "google_vertex"): 0.0005,
    # Replicate — invisible to this table until the 2026-08 audit, so
    # whisperX / forced-align / demucs spend never showed up at all
    # (real invoices: $3.67 may, $7.12 jun). Rates from the model pages.
    ("victor-upmeet/whisperx", "replicate"): 0.02,
    ("cureau/force-align-wordstamps", "replicate"): 0.007,
    ("cjwbw/demucs", "replicate"): 0.035,
    # Whisper local — runs on our compute, no API charge
    ("whisper", "local"): 0.0,
    ("whisper-large-v3", "local"): 0.0,
    # Whisper OpenAI API — billed at $0.006/min of audio. We pay per call,
    # but it bills per-minute; using an average song length of ~3.5 min
    # gives ~$0.021 per call. Cheap to bump if real audio lengths drift.
    ("whisper-1", "openai"): 0.021,
    ("whisper", "openai"): 0.021,
    # Gap rescue transcribes clips capped at 120 s, not a full ~3.5 min song.
    ("whisper-1-gap-rescue", "openai"): 0.012,
    # Small text-only formatter call (one numbered lyric payload per song).
    ("gpt-4o-mini", "openai"): 0.001,
    # Human-provided fallback — no AI cost
    ("human-provided", "user_upload"): 0.0,
}

# Fallback for tools we haven't priced yet. Conservative.
DEFAULT_COST_PER_CALL = 0.01


def cost_for_record(tool_name: str, tool_provider: str,
                    rates: dict[str, float] | None = None) -> float:
    """Cost of one AIProvenance record.

    `rates` are the per-tool rates DERIVED FROM THE INVOICE for the period
    being measured (see `rate_calibration.load_applied_rates`). When present
    they win over `COST_PER_CALL`, which is list price and drifts: Veo is
    carried at $0.80 and the real invoice divides out to ~$0.62, so every
    aggregate ran ~25% high.

    Storing the calibration without applying it here — which is what the
    first version of this did — makes the whole calibration module
    decorative: the panel would report a "real" rate it never used.
    """
    if rates:
        from rate_calibration import rate_for_tool
        derived = rate_for_tool(tool_name, rates)
        if derived is not None:
            return derived
    return COST_PER_CALL.get((tool_name, tool_provider), DEFAULT_COST_PER_CALL)


def rates_for_window(
    db,
    since: datetime,
    until: datetime | None = None,
) -> dict[str, dict[str, float]]:
    """Load every monthly calibration intersecting a reporting window.

    The dashboard supports rolling 30/90/365-day windows, so applying only
    the start month's discount/model mix can misprice almost the entire
    range. This loads one snapshot per calendar month up front; aggregate
    queries then group calls by month and select the matching rates without
    issuing a query per provenance row.
    """
    try:
        from rate_calibration import load_applied_rates

        end = until or datetime.now(timezone.utc)
        cursor = datetime(since.year, since.month, 1, tzinfo=timezone.utc)
        out: dict[str, dict[str, float]] = {}
        while cursor < end:
            period = f"{cursor.year:04d}-{cursor.month:02d}"
            loaded = load_applied_rates(db, period)
            if loaded:
                out[period] = loaded
            if cursor.month == 12:
                cursor = datetime(cursor.year + 1, 1, 1, tzinfo=timezone.utc)
            else:
                cursor = datetime(
                    cursor.year, cursor.month + 1, 1, tzinfo=timezone.utc,
                )
        return out
    except Exception:
        return {}


def _rates_for_month(rates: dict | None, year, month) -> dict[str, float]:
    """Accept new period maps plus legacy flat rates supplied by callers."""
    if not rates:
        return {}
    if all(not isinstance(v, dict) for v in rates.values()):
        return rates
    period = f"{int(year):04d}-{int(month):02d}"
    selected = rates.get(period) or {}
    return selected if isinstance(selected, dict) else {}


def _provenance_month_columns():
    """Portable SQL month bucket (PostgreSQL and SQLite test DB)."""
    return (
        extract("year", AIProvenance.created_at).label("rate_year"),
        extract("month", AIProvenance.created_at).label("rate_month"),
    )


# ---------------------------------------------------------------------------
# Billable-row filter
# ---------------------------------------------------------------------------

# `response_summary` prefix written by pipeline._generate_veo_video when the
# R2 cache served the clip. The recorder is opened before the cache lookup,
# so the row exists but no provider call was made and nothing was billed.
CACHE_HIT_PREFIX = "cache_hit"


def billable_filter():
    """SQLAlchemy criterion selecting provenance rows we actually paid for.

    Excludes cache hits. Deliberately KEEPS error rows and in-flight rows
    (`response_summary IS NULL`): a Veo generation that succeeded upstream
    but whose download/worker died is still billed by the provider, and we
    would rather over-report cost than discover the gap on the invoice.
    `cost_dashboard_global` surfaces those two buckets separately so the
    uncertainty is visible instead of buried.
    """
    return or_(
        AIProvenance.response_summary.is_(None),
        not_(AIProvenance.response_summary.like(f"{CACHE_HIT_PREFIX}%")),
    )


# Job statuses that represent a video we can actually invoice. Mirrors
# `deliverable` in cost_dashboard_global.
DELIVERED_STATUSES = ("done", "pending_review")

# Human labels per status. Delivered statuses get distinct labels so the
# panel doesn't render two identical "entregado" rows — both count as
# delivered, but `pending_review` is still awaiting client approval and
# can yet turn into a rejection.
_DELIVERED_LABELS = {
    "done": "entregado",
    "pending_review": "entregado (a revisar)",
    "delivered_reopened": "entregado (reabierto)",
}

# Job statuses whose spend produced nothing shippable, split so the panel
# can name the failure mode rather than lumping it into "other".
_WASTE_LABELS = {
    "bg_preview_done": "preview_descartado",
    "bg_preview_failed": "preview_fallido",
    "rejected": "rechazado",
    "failed": "fallido",
    "error": "error",
    "transcription_failed": "transcripcion_fallida",
}


def retained_delivery_filter():
    """SQL predicate for a shipped job currently inside/after an edit.

    ``completed_at < editing_started_at`` is the durable evidence: /edit
    preserves the old completion for delivered jobs, but clears it for an
    undelivered rejection. If that rescue later fails, the failure timestamp
    is newer than editing_started_at and therefore does not become a delivery.
    """
    return and_(
        ~Job.status.in_(DELIVERED_STATUSES),
        Job.editing_started_at.isnot(None),
        Job.completed_at.isnot(None),
        Job.completed_at < Job.editing_started_at,
    )


def delivered_job_filter():
    """SQL predicate for current or retained historical delivery state."""
    return or_(Job.status.in_(DELIVERED_STATUSES), retained_delivery_filter())


def job_was_delivered(status: str | None, completed_at, editing_started_at) -> bool:
    """Python twin of :func:`delivered_job_filter` for materialized rows."""
    if status in DELIVERED_STATUSES:
        return True
    if completed_at is None or editing_started_at is None:
        return False
    return completed_at < editing_started_at


def cost_waste_breakdown(db: Session, since_days: int = 30,
                         tenant_id: str | None = None,
                         start: datetime | None = None,
                         end: datetime | None = None,
                         rates: dict | None = None) -> dict:
    """Attribute billable AI spend to what it actually produced.

    The headline number the 2026-08 audit surfaced: in jul-2026, 59% of
    billable Veo spend went to background previews the operator discarded
    and to jobs that ended up rejected — none of which became a delivered
    video, yet all of which were divided into "cost per video" as if they
    had. This splits spend by the destination of the job that incurred it.

    Returns:
        {
          "since": iso8601, "since_days": int,
          "total_cost": float,
          "delivered_cost": float, "wasted_cost": float,
          "waste_ratio": float | None,       # wasted / total
          "delivered_videos": int,
          "cost_per_delivered": float | None,    # total / delivered (honest)
          "cost_per_delivered_direct": float | None,  # delivered_cost only
          "by_destination": [
              {"destination", "status", "jobs", "calls", "cost"}, ...
          ],
        }

    `cost_per_delivered` charges the waste to the videos that shipped —
    that is the number to price against. `cost_per_delivered_direct` is
    the floor you would reach if the waste were eliminated; the gap
    between the two is the size of the prize.

    Pass `tenant_id` to scope to one client — useful for seeing which
    tenant's operators are burning previews, since the waste rate is a
    workflow habit and varies a lot between them.

    Pass `rates` when reading more than one environment. The calibration
    snapshot lives in whichever database it was computed against, so a peer
    session would load nothing and value its half at list price — the merged
    waste ratio would then mix two valuation bases.
    """
    # `start`/`end` explícitos ganan sobre `since_days`. Sin ellos, la
    # ventana termina HOY — correcto para el panel en vivo, pero MAL para
    # pedir un mes cerrado: preguntar por junio en agosto medía jul-ago y se
    # comparaba contra la factura de junio.
    since = start or (datetime.now(timezone.utc) - timedelta(days=since_days))
    until = end
    # Tarifas derivadas de la factura del período; {} cae a COST_PER_CALL.
    _rates = rates if rates is not None else rates_for_window(db, since, until)

    def _window(query):
        query = query.filter(AIProvenance.created_at >= since)
        return query.filter(AIProvenance.created_at < until) if until else query

    def _scoped(query):
        return query.filter(Job.tenant_id == tenant_id) if tenant_id else query

    # Keep the human-facing current statuses, but fold a job whose retained
    # timestamps prove prior delivery into one explicit delivered bucket.
    effective_status = case(
        (retained_delivery_filter(), "delivered_reopened"),
        else_=Job.status,
    )
    rate_year, rate_month = _provenance_month_columns()
    rows_q = _scoped(
        db.query(
            effective_status.label("effective_status"),
            AIProvenance.tool_name,
            AIProvenance.tool_provider,
            rate_year,
            rate_month,
            func.count(AIProvenance.id).label("calls"),
            func.count(func.distinct(Job.job_id)).label("jobs"),
        )
        .join(Job, Job.job_id == AIProvenance.job_id)
    )
    rows = _window(rows_q).filter(billable_filter()).group_by(
        effective_status, AIProvenance.tool_name, AIProvenance.tool_provider,
        rate_year, rate_month,
    ).all()

    # Aggregate per status first; a status spans many tools.
    per_status: dict[str, dict] = {}
    for status, tool_name, tool_provider, year, month, calls, _jobs in rows:
        agg = per_status.setdefault(status, {"calls": 0, "cost": 0.0})
        agg["calls"] += calls
        month_rates = _rates_for_month(_rates, year, month)
        agg["cost"] += calls * cost_for_record(
            tool_name, tool_provider, month_rates,
        )

    # Distinct job counts must be queried separately — summing the
    # per-tool distinct counts above would multiply-count a job that used
    # several tools.
    job_counts = dict(
        _window(_scoped(
            db.query(effective_status, func.count(func.distinct(Job.job_id)))
            .join(AIProvenance, Job.job_id == AIProvenance.job_id)
        )).filter(billable_filter()).group_by(effective_status).all()
    )

    by_destination = []
    total_cost = 0.0
    delivered_cost = 0.0
    for status, agg in per_status.items():
        is_delivered = status in (*DELIVERED_STATUSES, "delivered_reopened")
        total_cost += agg["cost"]
        if is_delivered:
            delivered_cost += agg["cost"]
        by_destination.append({
            "destination": (
                _DELIVERED_LABELS.get(status, "entregado") if is_delivered
                else _WASTE_LABELS.get(status, f"descarte ({status})")
            ),
            "status": status,
            "delivered": is_delivered,
            "jobs": int(job_counts.get(status, 0)),
            "calls": agg["calls"],
            "cost": round(agg["cost"], 4),
        })
    by_destination.sort(key=lambda r: r["cost"], reverse=True)

    # Delivered video count comes from the jobs table, not from the
    # provenance join: a job that shipped without any billable AI call
    # (library background reused as-is) is still a delivered video and
    # must dilute the cost per video.
    #
    # Counted by `completed_at`, not `created_at`: a song started on the last
    # day of a month and finished in the next one belongs to the period it
    # SHIPPED in, which is the period the invoice is compared against. Using
    # created_at silently lands it in the wrong month and skews both months'
    # cost per video. `coalesce` covers older rows without the column.
    delivered_videos = int(
        _scoped(
            db.query(func.count(Job.id))
            .filter(func.coalesce(Job.completed_at, Job.created_at) >= since)
            .filter(delivered_job_filter())
        ).filter(
            func.coalesce(Job.completed_at, Job.created_at) < until
        ).scalar() or 0
    ) if until else int(
        _scoped(
            db.query(func.count(Job.id))
            .filter(func.coalesce(Job.completed_at, Job.created_at) >= since)
            .filter(delivered_job_filter())
        ).scalar() or 0
    )

    wasted_cost = total_cost - delivered_cost
    return {
        "since": since.isoformat(),
        "since_days": since_days,
        "tenant_id": tenant_id,
        "total_cost": round(total_cost, 4),
        "delivered_cost": round(delivered_cost, 4),
        "wasted_cost": round(wasted_cost, 4),
        "waste_ratio": round(wasted_cost / total_cost, 4) if total_cost else None,
        "delivered_videos": delivered_videos,
        "cost_per_delivered": (
            round(total_cost / delivered_videos, 4) if delivered_videos else None
        ),
        "cost_per_delivered_direct": (
            round(delivered_cost / delivered_videos, 4)
            if delivered_videos else None
        ),
        "by_destination": by_destination,
        "environments": 1,
    }


def merge_waste_breakdowns(*breakdowns: dict) -> dict:
    """Suma varios `cost_waste_breakdown` en uno, para el mismo período.

    La producción gestionada de UMG corre en STAGING mientras la factura y
    el conteo de entregas cubren los dos entornos: devolver un desperdicio
    de un solo entorno al lado de totales de dos hacía que `waste_ratio` y
    `delivered_videos` describieran una porción del negocio y parecieran
    describirlo entero.
    """
    breakdowns = tuple(b for b in breakdowns if b)
    if not breakdowns:
        return {}
    if len(breakdowns) == 1:
        return breakdowns[0]

    total = sum(b.get("total_cost") or 0.0 for b in breakdowns)
    delivered_cost = sum(b.get("delivered_cost") or 0.0 for b in breakdowns)
    delivered_videos = sum(int(b.get("delivered_videos") or 0)
                           for b in breakdowns)
    wasted = total - delivered_cost

    por_estado: dict[str, dict] = {}
    for b in breakdowns:
        for row in b.get("by_destination") or []:
            acc = por_estado.setdefault(row["status"], {
                "destination": row["destination"], "status": row["status"],
                "delivered": row["delivered"], "jobs": 0, "calls": 0,
                "cost": 0.0,
            })
            acc["jobs"] += int(row.get("jobs") or 0)
            acc["calls"] += int(row.get("calls") or 0)
            acc["cost"] += float(row.get("cost") or 0.0)
    filas = sorted(por_estado.values(), key=lambda r: -r["cost"])
    for r in filas:
        r["cost"] = round(r["cost"], 4)

    base = breakdowns[0]
    return {
        "since": base.get("since"),
        "since_days": base.get("since_days"),
        "tenant_id": base.get("tenant_id"),
        "total_cost": round(total, 4),
        "delivered_cost": round(delivered_cost, 4),
        "wasted_cost": round(wasted, 4),
        "waste_ratio": round(wasted / total, 4) if total else None,
        "delivered_videos": delivered_videos,
        "cost_per_delivered": (round(total / delivered_videos, 4)
                               if delivered_videos else None),
        "cost_per_delivered_direct": (round(delivered_cost / delivered_videos, 4)
                                      if delivered_videos else None),
        "by_destination": filas,
        "environments": sum(int(b.get("environments") or 1)
                            for b in breakdowns),
    }


def tenant_cost_summary(
    db: Session,
    tenant_id: str,
    since_days: int = 30,
) -> dict:
    """Summarize AI cost for one tenant over the last `since_days` days.

    Returns:
        {
          "tenant_id": str,
          "since": iso8601 timestamp,
          "total_cost": float (USD),
          "total_calls": int,
          "by_tool": [{"tool_name", "tool_provider", "calls", "cost"}, ...],
        }

    Joins AIProvenance with Job to filter by tenant_id.
    """
    since = datetime.now(timezone.utc) - timedelta(days=since_days)
    _rates = rates_for_window(db, since)
    rate_year, rate_month = _provenance_month_columns()

    rows = (
        db.query(
            AIProvenance.tool_name,
            AIProvenance.tool_provider,
            rate_year,
            rate_month,
            func.count(AIProvenance.id).label("calls"),
        )
        .join(Job, Job.job_id == AIProvenance.job_id)
        .filter(Job.tenant_id == tenant_id)
        .filter(AIProvenance.created_at >= since)
        .filter(billable_filter())
        .group_by(
            AIProvenance.tool_name, AIProvenance.tool_provider,
            rate_year, rate_month,
        )
        .all()
    )

    tool_totals: dict[tuple[str, str], dict] = {}
    total_cost = 0.0
    total_calls = 0
    for tool_name, tool_provider, year, month, calls in rows:
        rate = cost_for_record(
            tool_name, tool_provider, _rates_for_month(_rates, year, month),
        )
        cost = calls * rate
        total_cost += cost
        total_calls += calls
        agg = tool_totals.setdefault((tool_name, tool_provider), {
            "tool_name": tool_name,
            "tool_provider": tool_provider,
            "calls": 0,
            "cost": 0.0,
        })
        agg["calls"] += calls
        agg["cost"] += cost

    by_tool = []
    for agg in tool_totals.values():
        calls = agg["calls"]
        cost = agg["cost"]
        by_tool.append({
            **agg,
            "rate_per_call": round(cost / calls, 6) if calls else 0.0,
            "cost": round(cost, 4),
        })

    by_tool.sort(key=lambda r: r["cost"], reverse=True)

    return {
        "tenant_id": tenant_id,
        "since": since.isoformat(),
        "since_days": since_days,
        "total_cost": round(total_cost, 4),
        "total_calls": total_calls,
        "by_tool": by_tool,
    }


# Provider buckets for the global dashboard. Maps the tool_provider /
# tool_name prefix to a short user-visible label. Keep aligned with the
# Veo / Gemini / Whisper / Imagen split shown in the cost panel.
_PROVIDER_BUCKETS = (
    ("veo",     lambda n, p: n.startswith("veo-")),
    ("gemini",  lambda n, p: n.startswith("gemini-")),
    ("imagen",  lambda n, p: n.startswith("imagen-")),
    ("whisper", lambda n, p: n.startswith("whisper") and p in ("openai", "local")),
    ("other",   lambda n, p: True),  # catch-all, must stay last
)


def _bucket_for(tool_name: str, tool_provider: str) -> str:
    for label, predicate in _PROVIDER_BUCKETS:
        if predicate(tool_name, tool_provider):
            return label
    return "other"


def cost_dashboard_global(db: Session, since_days: int = 30,
                          revenue_per_video_usd: float = 8.0) -> dict:
    """Margin-style cost dashboard across all tenants.

    Powers /admin/margin. Returns enough data for the operator panel to
    show: total AI spend, per-provider breakdown, video counts (done /
    pending_review / rejected / error), rejection rate, cost per
    deliverable, and a margin estimate against an assumed revenue per
    video. `revenue_per_video_usd` is just for the margin display — it
    does not affect cost numbers.

    Whisper calls are priced via the COST_PER_CALL table — the row
    `("whisper-1", "openai")` is what catches our prod transcriptions.
    Local Whisper stays at 0 (matches actual marginal cost).
    """
    since = datetime.now(timezone.utc) - timedelta(days=since_days)
    _rates = rates_for_window(db, since)
    rate_year, rate_month = _provenance_month_columns()

    # --- AI spend by tool/provider (billable rows only) ---
    rows = (
        db.query(
            AIProvenance.tool_name,
            AIProvenance.tool_provider,
            rate_year,
            rate_month,
            func.count(AIProvenance.id).label("calls"),
        )
        .filter(AIProvenance.created_at >= since)
        .filter(billable_filter())
        .group_by(
            AIProvenance.tool_name, AIProvenance.tool_provider,
            rate_year, rate_month,
        )
        .all()
    )

    # --- Row-quality counters -------------------------------------------
    # Cache hits are excluded from spend above; errored and in-flight rows
    # are INCLUDED (we may well have been billed). Surfacing all three
    # lets the operator see how much of the total is uncertain instead of
    # trusting a single opaque number.
    cache_hits = int(
        db.query(func.count(AIProvenance.id))
        .filter(AIProvenance.created_at >= since)
        .filter(AIProvenance.response_summary.like(f"{CACHE_HIT_PREFIX}%"))
        .scalar() or 0
    )
    errored = int(
        db.query(func.count(AIProvenance.id))
        .filter(AIProvenance.created_at >= since)
        .filter(AIProvenance.response_summary.like("error%"))
        .scalar() or 0
    )
    in_flight = int(
        db.query(func.count(AIProvenance.id))
        .filter(AIProvenance.created_at >= since)
        .filter(AIProvenance.response_summary.is_(None))
        .scalar() or 0
    )

    tool_totals: dict[tuple[str, str], dict] = {}
    by_provider: dict[str, dict] = {}
    total_cost = 0.0
    total_calls = 0
    for tool_name, tool_provider, year, month, calls in rows:
        rate = cost_for_record(
            tool_name, tool_provider, _rates_for_month(_rates, year, month),
        )
        cost = calls * rate
        bucket = _bucket_for(tool_name, tool_provider)
        total_cost += cost
        total_calls += calls
        tool_agg = tool_totals.setdefault((tool_name, tool_provider), {
            "tool_name": tool_name,
            "tool_provider": tool_provider,
            "calls": 0,
            "cost": 0.0,
            "provider_bucket": bucket,
        })
        tool_agg["calls"] += calls
        tool_agg["cost"] += cost
        agg = by_provider.setdefault(
            bucket, {"calls": 0, "cost": 0.0}
        )
        agg["calls"] += calls
        agg["cost"] += cost

    by_tool = []
    for agg in tool_totals.values():
        calls = agg["calls"]
        cost = agg["cost"]
        by_tool.append({
            **agg,
            "rate_per_call": round(cost / calls, 6) if calls else 0.0,
            "cost": round(cost, 4),
        })
    by_tool.sort(key=lambda r: r["cost"], reverse=True)
    by_provider_list = sorted(
        [{"provider": k, **v, "cost": round(v["cost"], 4)}
         for k, v in by_provider.items()],
        key=lambda r: r["cost"],
        reverse=True,
    )

    # --- Video counts (same window so the cost-per-video math is honest) ---
    video_counts = dict(
        db.query(Job.status, func.count(Job.id))
        .filter(Job.created_at >= since)
        .group_by(Job.status)
        .all()
    )
    done = int(video_counts.get("done", 0))
    pending = int(video_counts.get("pending_review", 0))
    rejected = int(video_counts.get("rejected", 0))
    error = int(video_counts.get("error", 0))
    finished = done + pending + rejected + error
    deliverable = done + pending

    # Avoid divide-by-zero in fresh tenants / very short windows.
    cost_per_done = round(total_cost / done, 4) if done else None
    cost_per_deliverable = (
        round(total_cost / deliverable, 4) if deliverable else None
    )
    rejection_rate = round(rejected / finished, 4) if finished else None

    # Margin against a revenue assumption. Pure display math — caller
    # passes revenue_per_video_usd; default $8 reflects the Universal
    # contract ($2,000 / 250 videos).
    margin_per_video = None
    margin_total = None
    if cost_per_deliverable is not None and revenue_per_video_usd > 0:
        margin_per_video = round(
            revenue_per_video_usd - cost_per_deliverable, 4
        )
        margin_total = round(
            (revenue_per_video_usd - cost_per_deliverable) * deliverable, 2
        )

    # --- Per-tenant breakdown ---
    # Cost: sum cost_for_record across each tenant's provenance rows.
    tenant_rows = (
        db.query(
            Job.tenant_id,
            AIProvenance.tool_name,
            AIProvenance.tool_provider,
            rate_year,
            rate_month,
            func.count(AIProvenance.id).label("calls"),
        )
        .join(Job, Job.job_id == AIProvenance.job_id)
        .filter(AIProvenance.created_at >= since)
        .filter(billable_filter())
        .group_by(
            Job.tenant_id, AIProvenance.tool_name,
            AIProvenance.tool_provider, rate_year, rate_month,
        )
        .all()
    )
    tenant_cost: dict[str, dict] = {}
    for tenant_id, tool_name, tool_provider, year, month, calls in tenant_rows:
        agg = tenant_cost.setdefault(
            tenant_id, {"calls": 0, "cost": 0.0}
        )
        rate = cost_for_record(
            tool_name, tool_provider, _rates_for_month(_rates, year, month),
        )
        agg["calls"] += calls
        agg["cost"] += calls * rate

    # Video status counts per tenant (same window).
    tenant_status_rows = (
        db.query(Job.tenant_id, Job.status, func.count(Job.id))
        .filter(Job.created_at >= since)
        .group_by(Job.tenant_id, Job.status)
        .all()
    )
    tenant_status: dict[str, dict[str, int]] = {}
    for tenant_id, status, n in tenant_status_rows:
        tenant_status.setdefault(tenant_id, {})[status] = int(n)

    # Union of tenants seen in spend OR jobs so a 0-cost tenant with
    # jobs still shows up (and vice versa).
    by_tenant = []
    for tid in set(tenant_cost.keys()) | set(tenant_status.keys()):
        spend = tenant_cost.get(tid, {"calls": 0, "cost": 0.0})
        sts = tenant_status.get(tid, {})
        t_done = int(sts.get("done", 0))
        t_pending = int(sts.get("pending_review", 0))
        t_rejected = int(sts.get("rejected", 0))
        t_error = int(sts.get("error", 0))
        t_finished = t_done + t_pending + t_rejected + t_error
        t_deliverable = t_done + t_pending
        by_tenant.append({
            "tenant_id": tid,
            "calls": spend["calls"],
            "cost": round(spend["cost"], 4),
            "done": t_done,
            "pending_review": t_pending,
            "rejected": t_rejected,
            "error": t_error,
            "deliverable": t_deliverable,
            "cost_per_deliverable": (
                round(spend["cost"] / t_deliverable, 4)
                if t_deliverable else None
            ),
            "rejection_rate": (
                round(t_rejected / t_finished, 4) if t_finished else None
            ),
        })
    by_tenant.sort(key=lambda r: r["cost"], reverse=True)

    # --- Per-user breakdown ---
    # Same pattern but grouped by Job.user_id + joined to User for the
    # display name. Users without finished jobs in the window get
    # filtered (no useful display).
    from database import User as UserModel
    user_rows = (
        db.query(
            Job.user_id,
            Job.tenant_id,
            UserModel.username,
            AIProvenance.tool_name,
            AIProvenance.tool_provider,
            rate_year,
            rate_month,
            func.count(AIProvenance.id).label("calls"),
        )
        .join(Job, Job.job_id == AIProvenance.job_id)
        .outerjoin(UserModel, UserModel.id == Job.user_id)
        .filter(AIProvenance.created_at >= since)
        .filter(billable_filter())
        .group_by(Job.user_id, Job.tenant_id, UserModel.username,
                  AIProvenance.tool_name, AIProvenance.tool_provider,
                  rate_year, rate_month)
        .all()
    )
    user_cost: dict = {}
    for (user_id, tenant_id, username, tool_name, tool_provider,
         year, month, calls) in user_rows:
        key = (user_id, tenant_id)
        agg = user_cost.setdefault(key, {
            "user_id": user_id,
            "username": username,
            "tenant_id": tenant_id,
            "calls": 0,
            "cost": 0.0,
        })
        rate = cost_for_record(
            tool_name, tool_provider, _rates_for_month(_rates, year, month),
        )
        agg["calls"] += calls
        agg["cost"] += calls * rate

    user_status_rows = (
        db.query(Job.user_id, Job.tenant_id, Job.status, func.count(Job.id))
        .filter(Job.created_at >= since)
        .group_by(Job.user_id, Job.tenant_id, Job.status)
        .all()
    )
    user_status: dict = {}
    for user_id, tenant_id, status, n in user_status_rows:
        key = (user_id, tenant_id)
        user_status.setdefault(key, {})[status] = int(n)

    by_user = []
    for key in set(user_cost.keys()) | set(user_status.keys()):
        spend = user_cost.get(key, {
            "user_id": key[0],
            "username": None,
            "tenant_id": key[1],
            "calls": 0,
            "cost": 0.0,
        })
        sts = user_status.get(key, {})
        u_done = int(sts.get("done", 0))
        u_pending = int(sts.get("pending_review", 0))
        u_rejected = int(sts.get("rejected", 0))
        u_error = int(sts.get("error", 0))
        u_finished = u_done + u_pending + u_rejected + u_error
        u_deliverable = u_done + u_pending
        by_user.append({
            "user_id": spend["user_id"],
            "username": spend["username"],
            "tenant_id": spend["tenant_id"],
            "calls": spend["calls"],
            "cost": round(spend["cost"], 4),
            "done": u_done,
            "pending_review": u_pending,
            "rejected": u_rejected,
            "error": u_error,
            "deliverable": u_deliverable,
            "cost_per_deliverable": (
                round(spend["cost"] / u_deliverable, 4)
                if u_deliverable else None
            ),
            "rejection_rate": (
                round(u_rejected / u_finished, 4) if u_finished else None
            ),
        })
    by_user.sort(key=lambda r: r["cost"], reverse=True)

    return {
        "since": since.isoformat(),
        "since_days": since_days,
        "total_cost": round(total_cost, 4),
        "total_calls": total_calls,
        "by_tool": by_tool,
        "by_provider": by_provider_list,
        "by_tenant": by_tenant,
        "by_user": by_user,
        "video_counts": {
            "done": done,
            "pending_review": pending,
            "rejected": rejected,
            "error": error,
            "finished": finished,
            "deliverable": deliverable,
        },
        "cost_per_done": cost_per_done,
        "cost_per_deliverable": cost_per_deliverable,
        "rejection_rate": rejection_rate,
        "revenue_per_video_usd": revenue_per_video_usd,
        "margin_per_video": margin_per_video,
        "margin_total": margin_total,
        # Row-quality counters. `cache_hits` are already excluded from
        # total_cost; `errored` and `in_flight` are included and represent
        # the uncertain slice of the total.
        "row_quality": {
            "cache_hits_excluded": cache_hits,
            "errored_included": errored,
            "in_flight_included": in_flight,
        },
        # Where the money actually went. See cost_waste_breakdown().
        "waste": cost_waste_breakdown(db, since_days=since_days),
    }


def record_ai_call(
    job_id: str,
    step: str,
    tool_name: str,
    tool_provider: str,
    prompt: str,
    input_data_types: list[str] = None,
    tool_version: str = None,
):
    """Start recording an AI call. Returns a ProvenanceRecorder.

    Usage:
        recorder = record_ai_call(job_id, "video_bg", "veo-3.1-generate-001", ...)
        # ... make the API call ...
        recorder.finish(response_summary="...", output_artifact="/path/to/file.mp4")
    """
    return ProvenanceRecorder(
        job_id=job_id,
        step=step,
        tool_name=tool_name,
        tool_provider=tool_provider,
        prompt=prompt,
        input_data_types=input_data_types,
        tool_version=tool_version,
    )


# ai_provenance VARCHAR limits (mirror database.py). A caller passing an
# over-long value used to crash the start-row INSERT with
# StringDataRightTruncation — and because the insert is wrapped in a
# best-effort try/except, the ENTIRE provenance row was then dropped
# silently (UMG compliance gap). We now truncate to fit and log WHICH
# field overflowed, so the audit row survives AND the offending caller is
# identifiable instead of invisible. (Sentry "Failed to insert provenance
# start row: value too long", 2026-06-01.)
_PROV_MAXLEN = {"step": 50, "tool_name": 100, "tool_provider": 50, "tool_version": 100}


def _fit_varchar(value, field: str, job_id) -> str | None:
    """Truncate a provenance VARCHAR field to its column limit, warning on
    overflow. None passes through unchanged."""
    if value is None:
        return None
    s = str(value)
    limit = _PROV_MAXLEN[field]
    if len(s) > limit:
        logger.warning(
            "[PROVENANCE] %s=%r (%d chars) exceeds varchar(%d) for job %s — "
            "truncating to fit (previously this dropped the whole audit row)",
            field, s[:60] + "…", len(s), limit, job_id,
        )
        return s[:limit]
    return s


class ProvenanceRecorder:
    """Records a single AI tool invocation with timing.

    The row is INSERTED at construction (with response_summary/duration_ms
    left null to mark the call as in-flight) and UPDATED at finish().
    Pre-fix the row was inserted only at finish(), so any worker crash
    between record_ai_call(...) and recorder.finish(...) — Veo polling
    timeout, OOM kill, network error during download — left no trace of
    the API call in the audit trail. Now the call is always recorded;
    in-flight rows simply have null duration_ms / response_summary.

    Also a context manager: `with record_ai_call(...) as rec:` will call
    finish() automatically, including a synthetic error summary on the
    exception path.
    """

    def __init__(self, job_id, step, tool_name, tool_provider, prompt,
                 input_data_types=None, tool_version=None):
        self.job_id = job_id
        self.step = step
        self.tool_name = tool_name
        self.tool_provider = tool_provider
        self.prompt = prompt
        self.input_data_types = input_data_types
        self.tool_version = tool_version
        self.start_time = time.time()
        self._row_id: int | None = None
        self._finished = False

        # Insert the "started" row immediately. If the DB hiccups here we
        # log and continue with _row_id=None; finish() then becomes a
        # no-op so provenance bookkeeping never raises out of the worker.
        db = SessionLocal()
        try:
            # prompt_sent is NOT NULL; coalesce None → "" so a caller that
            # forgot the prompt still records the row instead of dropping it.
            prompt_text = self.prompt or ""
            record = AIProvenance(
                job_id=self.job_id,
                step=_fit_varchar(self.step, "step", self.job_id),
                tool_name=_fit_varchar(self.tool_name, "tool_name", self.job_id),
                tool_provider=_fit_varchar(self.tool_provider, "tool_provider", self.job_id),
                tool_version=_fit_varchar(self.tool_version, "tool_version", self.job_id),
                prompt_sent=prompt_text,
                prompt_hash=hashlib.sha256(prompt_text.encode()).hexdigest(),
                response_summary=None,
                input_data_types=self.input_data_types,
                output_artifact=None,
                duration_ms=None,
            )
            db.add(record)
            db.commit()
            self._row_id = record.id
        except Exception as e:
            logger.error(f"Failed to insert provenance start row: {e}")
            db.rollback()
        finally:
            db.close()

    def finish(self, response_summary: str = None, output_artifact: str = None):
        """Update the in-flight provenance row with end-of-call data.

        Idempotent — calling finish() twice is safe. No-op when the
        initial INSERT failed (self._row_id is None).
        """
        if self._finished or self._row_id is None:
            self._finished = True
            return
        self._finished = True
        duration_ms = int((time.time() - self.start_time) * 1000)
        db = SessionLocal()
        try:
            updated = (
                db.query(AIProvenance)
                .filter(AIProvenance.id == self._row_id)
                .update(
                    {
                        "response_summary": response_summary[:2000] if response_summary else None,
                        # output_artifact is varchar(500): long R2 keys /
                        # paths/URLs must not crash the finish() update.
                        "output_artifact": (str(output_artifact)[:500]
                                            if output_artifact else None),
                        "duration_ms": duration_ms,
                    },
                    synchronize_session=False,
                )
            )
            db.commit()
            if updated:
                logger.info(
                    f"Provenance recorded: job={self.job_id} step={self.step} "
                    f"tool={self.tool_name} duration={duration_ms}ms"
                )
        except Exception as e:
            logger.error(f"Failed to update provenance row: {e}")
            db.rollback()
        finally:
            db.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._finished:
            return False
        if exc is not None:
            try:
                self.finish(response_summary=f"error: {exc!r}")
            except Exception:
                pass
        else:
            self.finish()
        return False  # never swallow exceptions
