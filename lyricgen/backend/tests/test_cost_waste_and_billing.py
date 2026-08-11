"""Cost-accounting correctness — the 2026-08 audit contract.

Two defects this file locks down, both found by reconciling the modeled
cost against real invoices:

1. **Cache hits were billed.** `_generate_veo_video` opens the provenance
   recorder before the R2 cache lookup, so a cache hit writes a full row.
   Pricing those rows at $0.80 inflated jul-2026 spend by 40 of 248 Veo
   rows (~19%).
2. **Spend was divided by jobs created.** That hid the fact that 59% of
   jul-2026 Veo spend went to discarded background previews and rejected
   jobs, making cost/video look ~2.6x cheaper than it was.
"""

import pytest

import billing_sources
from provenance import (
    cost_for_record,
    cost_waste_breakdown,
    tenant_cost_summary,
)

# Every tenant this module writes under. The `db` fixture rolls back, but
# these tests commit (the aggregates run their own sessions), so rollback
# is a no-op and rows would leak into whatever runs next — several admin
# metrics tests count jobs globally and would start failing depending on
# file order. Clean up explicitly instead of relying on ordering.
_TEST_TENANTS = (
    "cache-tenant", "err-tenant", "waste-mix", "waste-cache",
    "waste-lib", "scope-a", "scope-b", "inflight-quality",
)


@pytest.fixture(autouse=True)
def _cleanup_test_rows(db):
    yield
    from database import AIProvenance, Job
    job_ids = [
        r[0] for r in
        db.query(Job.job_id).filter(Job.tenant_id.in_(_TEST_TENANTS)).all()
    ]
    if job_ids:
        (db.query(AIProvenance)
           .filter(AIProvenance.job_id.in_(job_ids))
           .delete(synchronize_session=False))
        (db.query(Job)
           .filter(Job.job_id.in_(job_ids))
           .delete(synchronize_session=False))
        db.commit()


def _job(db, job_id, status, tenant="waste-mix"):
    from database import Job
    db.add(Job(job_id=job_id, user_id=1, tenant_id=tenant,
               artist="A", filename="a.mp3", status=status))
    db.flush()


def _veo(db, job_id, summary=None, n=1):
    """Add `n` Veo provenance rows. `summary` mimics response_summary."""
    from database import AIProvenance
    for _ in range(n):
        db.add(AIProvenance(job_id=job_id, step="video_bg",
                            tool_name="veo-3.1-fast-generate-001",
                            tool_provider="google_vertex",
                            prompt_sent="p", response_summary=summary))


# ---------------------------------------------------------------------------
# Rate table
# ---------------------------------------------------------------------------

def test_veo_fast_rate_matches_invoice_reconciliation():
    # $0.10/s x 8s clip. Validated against the jun-2026 Google Cloud
    # invoice: 200 billable Veo rows modeled $160 of a $199.53 bill, the
    # remainder being staging + Imagen/Gemini tokens.
    assert cost_for_record("veo-3.1-fast-generate-001", "google_vertex") == 0.80


def test_imagen_4_is_priced_not_defaulted():
    """Imagen 4 is the runtime default but was absent from the table, so
    every call silently fell through to DEFAULT_COST_PER_CALL ($0.01)."""
    from provenance import DEFAULT_COST_PER_CALL
    for model in ("imagen-4.0-generate-001", "imagen-4.0-ultra-generate-001"):
        assert cost_for_record(model, "google_vertex") > DEFAULT_COST_PER_CALL


def test_replicate_models_are_priced():
    """Replicate spend ($3.67 may, $7.12 jun on real invoices) was fully
    invisible to the dashboard before the audit."""
    assert cost_for_record("victor-upmeet/whisperx", "replicate") > 0
    assert cost_for_record("cureau/force-align-wordstamps", "replicate") > 0


# ---------------------------------------------------------------------------
# Cache hits are not billable
# ---------------------------------------------------------------------------

def test_cache_hit_rows_are_excluded_from_cost(db):
    _job(db, "cache1", "done", tenant="cache-tenant")
    _veo(db, "cache1", summary=None, n=1)                    # billable
    _veo(db, "cache1", summary="cache_hit: 4.2MB key=abc", n=3)  # free
    db.commit()

    s = tenant_cost_summary(db, tenant_id="cache-tenant", since_days=30)
    # Only the one real generation is charged, not all four rows.
    assert s["total_calls"] == 1
    assert s["total_cost"] == 0.80


def test_errored_and_in_flight_rows_stay_billable(db):
    """A generation that succeeded upstream but whose worker died is still
    billed by the provider. Better to over-report than to be surprised by
    the invoice — so these rows are deliberately NOT filtered."""
    _job(db, "err1", "done", tenant="err-tenant")
    _veo(db, "err1", summary="error: TimeoutError()", n=1)
    _veo(db, "err1", summary=None, n=1)
    db.commit()

    s = tenant_cost_summary(db, tenant_id="err-tenant", since_days=30)
    assert s["total_calls"] == 2
    assert s["total_cost"] == 1.60


def test_row_quality_cuenta_solo_errores_incluidos_en_el_gasto(db):
    from provenance import (
        LEGACY_CONFIRMED_RATE_LIMIT_PREFIX,
        cost_dashboard_global,
    )

    before = cost_dashboard_global(db, since_days=30)["row_quality"][
        "errored_included"]
    _job(db, "errquality1", "done", tenant="error-quality")
    _veo(db, "errquality1", summary="error: ambiguous submission", n=1)
    _veo(db, "errquality1",
         summary=LEGACY_CONFIRMED_RATE_LIMIT_PREFIX, n=1)
    db.commit()

    after = cost_dashboard_global(db, since_days=30)["row_quality"][
        "errored_included"]
    assert after - before == 1


def test_row_quality_incluye_reservas_veo_sin_finalizar(db):
    from database import AIProvenance
    from provenance import (
        BUDGET_PENDING_PREFIX,
        BUDGET_RELEASED_PREFIX,
        BUDGET_RESERVED_PREFIX,
        cost_dashboard_global,
    )

    before = cost_dashboard_global(db, since_days=30)["row_quality"][
        "in_flight_included"]
    _job(db, "inflight1", "done", tenant="inflight-quality")
    for summary, duration in (
        (f"{BUDGET_RESERVED_PREFIX}: song=a|s", None),  # billable + active
        (None, None),                                  # legacy active row
        ("provider_ok", 1200),                         # finished
        (BUDGET_PENDING_PREFIX, None),                 # free candidate
        (BUDGET_RELEASED_PREFIX, None),                # confirmed free
    ):
        db.add(AIProvenance(
            job_id="inflight1", step="video_bg",
            tool_name="veo-3.1-fast-generate-001",
            tool_provider="google_vertex", prompt_sent="p",
            response_summary=summary, duration_ms=duration,
        ))
    db.commit()

    after = cost_dashboard_global(db, since_days=30)["row_quality"][
        "in_flight_included"]
    assert after - before == 2


# ---------------------------------------------------------------------------
# Waste attribution
# ---------------------------------------------------------------------------

def test_waste_breakdown_separates_delivered_from_discarded(db):
    """Reproduces the jul-2026 shape: spend on delivered videos, on
    discarded previews and on rejected jobs."""
    _job(db, "wd1", "done", tenant="waste-mix")
    _job(db, "wp1", "bg_preview_done", tenant="waste-mix")
    _job(db, "wr1", "rejected", tenant="waste-mix")
    _veo(db, "wd1", n=1)     # $0.80 delivered
    _veo(db, "wp1", n=2)     # $1.60 discarded preview
    _veo(db, "wr1", n=2)     # $1.60 rejected
    db.commit()

    w = cost_waste_breakdown(db, since_days=30, tenant_id="waste-mix")

    assert w["total_cost"] == 4.00
    assert w["delivered_cost"] == 0.80
    assert w["wasted_cost"] == 3.20
    assert w["waste_ratio"] == 0.80
    assert w["delivered_videos"] == 1
    # The honest number charges the waste to the video that shipped.
    assert w["cost_per_delivered"] == 4.00
    # The floor if the waste were eliminated.
    assert w["cost_per_delivered_direct"] == 0.80

    dest = {d["status"]: d for d in w["by_destination"]}
    assert dest["done"]["delivered"] is True
    assert dest["bg_preview_done"]["delivered"] is False
    assert dest["bg_preview_done"]["destination"] == "preview_descartado"
    assert dest["rejected"]["cost"] == 1.60


def test_waste_breakdown_ignores_cache_hits(db):
    _job(db, "wc1", "done", tenant="waste-cache")
    _veo(db, "wc1", n=1)
    _veo(db, "wc1", summary="cache_hit: 1MB", n=5)
    db.commit()

    w = cost_waste_breakdown(db, since_days=30, tenant_id="waste-cache")
    assert w["total_cost"] == 0.80


def test_delivered_video_without_ai_spend_still_dilutes_cost(db):
    """A job that shipped on a reused library background makes no billable
    call, but it is still a delivered video and must lower cost/video.
    Counting the denominator off the provenance join would miss it."""
    _job(db, "lib1", "done", tenant="waste-lib")   # no provenance rows at all
    _job(db, "gen1", "done", tenant="waste-lib")
    _veo(db, "gen1", n=1)
    db.commit()

    w = cost_waste_breakdown(db, since_days=30, tenant_id="waste-lib")
    assert w["delivered_videos"] == 2
    assert w["cost_per_delivered"] == 0.40      # 0.80 / 2, not 0.80 / 1


def test_waste_breakdown_unknown_tenant_does_not_divide_by_zero(db):
    w = cost_waste_breakdown(db, since_days=30, tenant_id="tenant-inexistente")
    assert w["total_cost"] == 0.0
    assert w["waste_ratio"] is None
    assert w["cost_per_delivered"] is None
    assert w["by_destination"] == []


def test_waste_breakdown_scopes_to_tenant(db):
    """Waste is a per-operator habit, so the panel needs it per client."""
    _job(db, "sa1", "bg_preview_done", tenant="scope-a")
    _job(db, "sb1", "bg_preview_done", tenant="scope-b")
    _veo(db, "sa1", n=1)
    _veo(db, "sb1", n=3)
    db.commit()

    a = cost_waste_breakdown(db, since_days=30, tenant_id="scope-a")
    b = cost_waste_breakdown(db, since_days=30, tenant_id="scope-b")
    assert a["wasted_cost"] == 0.80
    assert b["wasted_cost"] == 2.40


# ---------------------------------------------------------------------------
# billing_sources — must degrade, never raise
# ---------------------------------------------------------------------------

def test_period_bounds_handles_month_lengths():
    assert billing_sources._period_bounds("2026-02")[1].day == 28
    assert billing_sources._period_bounds("2026-07")[1].day == 31


def test_period_bounds_rejects_bad_month():

    with pytest.raises(ValueError):
        billing_sources._period_bounds("2026-13")


def test_unconfigured_source_reports_not_configured_not_zero(monkeypatch):
    """The critical distinction: a missing credential must never read as
    'this provider cost $0', which would silently understate total cost."""
    for var in ("GCP_BILLING_BQ_PROJECT", "GCP_BILLING_BQ_DATASET",
                "GCP_BILLING_BQ_TABLE"):
        monkeypatch.delenv(var, raising=False)

    result = billing_sources.fetch_gcp("2026-07")
    assert result.status == "not_configured"
    assert result.amount_usd is None          # NOT 0.0


def test_fetch_all_never_raises_and_reports_completeness(monkeypatch):
    """One exploding source must not take down the whole cost panel."""
    def boom(period):
        raise RuntimeError("provider on fire")

    monkeypatch.setitem(billing_sources.SOURCES, "gcp", boom)
    monkeypatch.delenv("RAILWAY_API_TOKEN", raising=False)

    out = billing_sources.fetch_all(period="2026-07",
                                    only=["gcp", "railway", "fixed"])

    assert "gcp" in out["errored"]
    assert "railway" in out["not_configured"]
    assert out["complete"] is False
    # The one healthy source still contributes.
    assert out["total_usd"] > 0


def test_fixed_subscriptions_sum_from_env(monkeypatch):
    monkeypatch.setenv("FIXED_SUBSCRIPTIONS_JSON",
                       '{"vercel_pro": 20, "github_pro": 4}')
    result = billing_sources.fetch_fixed("2026-07")
    assert result.status == "ok"
    assert result.amount_usd == 24.00


def test_fixed_subscriptions_bad_json_errors_not_crashes(monkeypatch):
    monkeypatch.setenv("FIXED_SUBSCRIPTIONS_JSON", "{not json")
    result = billing_sources.fetch_fixed("2026-07")
    assert result.status == "error"
    assert result.amount_usd is None


# ---------------------------------------------------------------------------
# Railway — dollars are derived, so the pricing math is the contract
# ---------------------------------------------------------------------------

def test_railway_prices_usage_metrics_against_real_invoice(monkeypatch):
    """Railway's GraphQL exposes no COST measurement — only raw resource
    metrics — so we price them ourselves. These are the real jun-2026
    numbers from the API; the model must land near the $124.54 invoice.

    Resource metrics arrive as unit-MINUTES over the window; NETWORK_TX_GB
    is a flow already in GB. Getting that distinction wrong is a ~700x
    error, hence this test.
    """
    real_june_usage = [
        {"measurement": "MEMORY_USAGE_GB", "value": 374188.80},
        {"measurement": "CPU_USAGE", "value": 10522.98},
        {"measurement": "DISK_USAGE_GB", "value": 219516.45},
        {"measurement": "BACKUP_USAGE_GB", "value": 28013.51},
        {"measurement": "NETWORK_TX_GB", "value": 673.35},
    ]

    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"data": {"usage": real_june_usage}}

    monkeypatch.setenv("RAILWAY_API_TOKEN", "fake")
    monkeypatch.setenv("RAILWAY_PROJECT_ID", "proj-genly")
    monkeypatch.setattr(billing_sources.requests, "post",
                        lambda *a, **k: _Resp())

    result = billing_sources.fetch_railway("2026-06")
    assert result.status == "ok"
    assert result.is_estimate is True
    # Invoice was $124.54; stay within 5%.
    assert 118.0 < result.amount_usd < 131.0, result.amount_usd

    by_measure = {b["measurement"]: b for b in result.breakdown}
    # Memory dominates, egress is second — if egress were divided by
    # minutes it would round to $0 and the total would collapse.
    assert by_measure["NETWORK_TX_GB"]["cost"] == round(673.35 * 0.05, 4)
    assert by_measure["MEMORY_USAGE_GB"]["cost"] > 80


def test_railway_uses_actual_days_in_month(monkeypatch):
    """A fixed 43200-minute divisor would misprice every month that isn't
    30 days — February would come out ~7% high."""
    usage = [{"measurement": "MEMORY_USAGE_GB", "value": 100000.0}]

    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"data": {"usage": usage}}

    monkeypatch.setenv("RAILWAY_API_TOKEN", "fake")
    monkeypatch.setattr(billing_sources.requests, "post",
                        lambda *a, **k: _Resp())

    feb = billing_sources.fetch_railway("2026-02")   # 28 days
    jul = billing_sources.fetch_railway("2026-07")   # 31 days
    # Same raw GB-minutes over a shorter month = more GB-months = costlier.
    assert feb.amount_usd > jul.amount_usd


def test_railway_empty_usage_is_incomplete_not_fake_zero(monkeypatch):
    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"data": {"usage": []}}

    monkeypatch.setenv("RAILWAY_API_TOKEN", "fake")
    monkeypatch.setattr(billing_sources.requests, "post",
                        lambda *a, **k: _Resp())
    result = billing_sources.fetch_railway("2026-07")
    assert result.status == "error"
    assert result.amount_usd is None


def test_railway_plan_es_minimo_no_cargo_aditivo(monkeypatch):
    # Uso real de $2/mes: la factura estimada debe ser el mínimo de $20, no
    # $2 de uso + $20 repetidos en la fuente de suscripciones.
    usage = [{"measurement": "CPU_USAGE", "value": 4320.0}]

    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"data": {"usage": usage}}

    monkeypatch.setenv("RAILWAY_API_TOKEN", "fake")
    monkeypatch.setenv("RAILWAY_PLAN_MINIMUM_USD", "20")
    monkeypatch.setattr(billing_sources.requests, "post",
                        lambda *a, **k: _Resp())
    result = billing_sources.fetch_railway("2026-06")
    assert result.status == "ok"
    assert result.amount_usd == 20.0
    assert result.raw["metered_usage_usd"] == pytest.approx(2.0)
    top_up = next(
        row for row in result.breakdown
        if row["measurement"] == "PLAN_MINIMUM_TOP_UP"
    )
    assert top_up["cost"] == pytest.approx(18.0)


def test_railway_no_imputa_minimo_account_wide_a_un_proyecto(monkeypatch):
    usage = [{"measurement": "CPU_USAGE", "value": 4320.0}]

    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"data": {"usage": usage}}

    monkeypatch.setenv("RAILWAY_API_TOKEN", "fake")
    monkeypatch.setenv("RAILWAY_PROJECT_ID", "proj-genly")
    monkeypatch.setenv("RAILWAY_PLAN_MINIMUM_USD", "20")
    monkeypatch.setattr(billing_sources.requests, "post",
                        lambda *a, **k: _Resp())

    result = billing_sources.fetch_railway("2026-06")
    assert result.amount_usd == pytest.approx(2.0)
    assert result.raw["plan_minimum_applied"] is False
    assert all(row["measurement"] != "PLAN_MINIMUM_TOP_UP"
               for row in result.breakdown)


def test_fixed_no_suma_railway_y_acepta_config_legacy(monkeypatch):
    monkeypatch.setenv(
        "FIXED_SUBSCRIPTIONS_JSON",
        '{"vercel_pro":20,"github_pro":4,"railway_plan":20}',
    )
    fixed = billing_sources.fetch_fixed("2026-07")
    assert fixed.amount_usd == 24.0
    assert all(row["concepto"] != "railway_plan" for row in fixed.breakdown)
    assert billing_sources._railway_plan_minimum_usd() == 20.0


# ---------------------------------------------------------------------------
# OpenAI — the org key is shared, so filtering is the contract
# ---------------------------------------------------------------------------

def test_openai_filters_to_genly_line_items(monkeypatch):
    """The OpenAI org is shared with unrelated projects: jul-2026 was
    $567.43 org-wide against $20.15 of whisper, which is all GenLy uses.
    Summing the org would overstate GenLy's cost ~28x."""
    payload = {
        "data": [{"results": [
            {"line_item": "whisper", "amount": {"value": 20.15}},
            {"line_item": "gpt-5.4-mini-2026-03-17, output",
             "amount": {"value": 309.71}},
            {"line_item": "gpt-image-1 image, output", "amount": {"value": 9.81}},
        ]}],
        "has_more": False,
    }

    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return payload

    monkeypatch.setenv("OPENAI_ADMIN_KEY", "sk-admin-fake")
    monkeypatch.setattr(billing_sources, "OPENAI_LINE_ITEM_FILTER", ["whisper"])
    monkeypatch.setattr(billing_sources.requests, "get", lambda *a, **k: _Resp())

    result = billing_sources.fetch_openai("2026-07")
    assert result.status == "ok"
    assert result.amount_usd == 20.15
    # Excluded lines stay visible so it's obvious what was filtered out.
    excluded = [b for b in result.breakdown if not b["incluido"]]
    assert len(excluded) == 2
    assert "otros proyectos" in result.detail


def test_openai_empty_filter_bills_whole_org(monkeypatch):
    payload = {
        "data": [{"results": [
            {"line_item": "whisper", "amount": {"value": 20.0}},
            {"line_item": "gpt-5.4", "amount": {"value": 80.0}},
        ]}],
        "has_more": False,
    }

    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return payload

    monkeypatch.setenv("OPENAI_ADMIN_KEY", "sk-admin-fake")
    monkeypatch.setattr(billing_sources, "OPENAI_LINE_ITEM_FILTER", [])
    monkeypatch.setattr(billing_sources.requests, "get", lambda *a, **k: _Resp())

    assert billing_sources.fetch_openai("2026-07").amount_usd == 100.0


def test_openai_incluye_formatter_y_whisper_pero_no_otros_proyectos(monkeypatch):
    payload = {
        "data": [{"results": [
            {"line_item": "whisper audio", "amount": {"value": 20.0}},
            {"line_item": "gpt-4o-mini input", "amount": {"value": 0.4}},
            {"line_item": "gpt-4o-mini output", "amount": {"value": 0.2}},
            {"line_item": "gpt-5.4 output", "amount": {"value": 80.0}},
        ]}],
        "has_more": False,
    }

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return payload

    monkeypatch.setenv("OPENAI_ADMIN_KEY", "sk-admin-fake")
    monkeypatch.setattr(
        billing_sources, "OPENAI_LINE_ITEM_FILTER", ["whisper", "gpt-4o-mini"],
    )
    monkeypatch.setattr(billing_sources.requests, "get", lambda *a, **k: _Resp())

    result = billing_sources.fetch_openai("2026-07")
    assert result.amount_usd == 20.6
    assert [row["line_item"] for row in result.breakdown if not row["incluido"]] == [
        "gpt-5.4 output",
    ]


@pytest.mark.parametrize("next_pages", [[None], ["cursor-1", "cursor-1"]])
def test_openai_rechaza_paginacion_que_no_puede_avanzar(
    monkeypatch, next_pages,
):
    payloads = [
        {"data": [], "has_more": True, "next_page": cursor}
        for cursor in next_pages
    ]

    class _Resp:
        def __init__(self, payload): self._payload = payload
        def raise_for_status(self): pass
        def json(self): return self._payload

    calls = iter(payloads)
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "sk-admin-fake")
    monkeypatch.setattr(
        billing_sources.requests, "get",
        lambda *a, **k: _Resp(next(calls)),
    )

    result = billing_sources.fetch_openai("2026-07")
    assert result.status == "error"
    assert result.amount_usd is None
    assert "paginación incompleta" in result.detail


def test_gcp_timeout_is_an_error_not_zero_dollars(monkeypatch):
    """BigQuery returns HTTP 200 with `jobComplete: false` and no rows when
    the query outruns `timeoutMs`. Reporting that as $0/ok would show ~half
    of all spend as free while `complete` stayed true — the exact failure
    this module exists to prevent. Scanning an unpartitioned billing export
    is slow enough that this is routine."""
    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"jobComplete": False}

    for k, v in (("GCP_BILLING_BQ_PROJECT", "p"),
                 ("GCP_BILLING_BQ_DATASET", "d"),
                 ("GCP_BILLING_BQ_TABLE", "t")):
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("GCP_BILLING_PROJECT_IDS", "genly-prod")
    monkeypatch.setattr(billing_sources, "_gcp_credentials", lambda: "tok")
    monkeypatch.setattr(billing_sources.requests, "post", lambda *a, **k: _Resp())

    result = billing_sources.fetch_gcp("2026-07")
    assert result.status == "error"
    assert result.amount_usd is None
    assert "jobComplete" in result.detail


def test_gcp_empty_result_is_an_error_not_zero_dollars(monkeypatch):
    """No rows usually means the export wasn't enabled yet (it is not
    retroactive) or the table name is wrong — never that GCP was free."""
    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"jobComplete": True, "rows": []}

    for k, v in (("GCP_BILLING_BQ_PROJECT", "p"),
                 ("GCP_BILLING_BQ_DATASET", "d"),
                 ("GCP_BILLING_BQ_TABLE", "t")):
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("GCP_BILLING_PROJECT_IDS", "genly-prod")
    monkeypatch.setattr(billing_sources, "_gcp_credentials", lambda: "tok")
    monkeypatch.setattr(billing_sources.requests, "post", lambda *a, **k: _Resp())

    result = billing_sources.fetch_gcp("2026-07")
    assert result.status == "error"
    assert result.amount_usd is None


def test_github_404_explains_missing_scope(monkeypatch):
    """A 404 here means the PAT lacks the `user` scope, not that the
    account doesn't exist — worth saying so, since the generic error
    sends you looking in the wrong place."""
    class _Resp:
        status_code = 404
        def raise_for_status(self): raise AssertionError("no debe llegar acá")
        def json(self): return {}

    monkeypatch.setenv("GITHUB_BILLING_TOKEN", "ghp_fake")
    monkeypatch.setenv("GITHUB_BILLING_USER", "alguien")
    monkeypatch.setattr(billing_sources.requests, "get", lambda *a, **k: _Resp())

    result = billing_sources.fetch_github(billing_sources.current_period())
    assert result.status == "error"
    assert "user" in result.detail
    assert result.amount_usd is None


def test_github_rechaza_meses_historicos(monkeypatch):
    """La API sólo devuelve el ciclo de facturación EN CURSO. Sin este guard,
    pedir un mes viejo devolvía los minutos de hoy y el snapshot los archivaba
    como si fueran de ese mes — un número inventado con cara de dato
    histórico."""
    monkeypatch.setenv("GITHUB_BILLING_TOKEN", "ghp_fake")
    monkeypatch.setenv("GITHUB_BILLING_USER", "alguien")
    # Si llegara a pegarle a la API, esto reventaría el test.
    monkeypatch.setattr(billing_sources.requests, "get",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("no debe consultar la API")))

    result = billing_sources.fetch_github("2019-01")
    assert result.status == "error"
    assert result.amount_usd is None
    assert "en curso" in result.detail


def test_github_no_imputa_ciclo_pago_a_mes_calendario(monkeypatch):
    """Un ciclo 15→15 no es el mes y no puede cerrar el total mensual."""
    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "included_minutes": 100,
                "total_minutes_used": 120,
                "total_paid_minutes_used": 20,
            }

    monkeypatch.setenv("GITHUB_BILLING_TOKEN", "ghp_fake")
    monkeypatch.setenv("GITHUB_BILLING_USER", "alguien")
    monkeypatch.setenv("GITHUB_BILLING_CYCLE_DAY", "15")
    monkeypatch.setattr(billing_sources.requests, "get", lambda *a, **k: _Resp())

    result = billing_sources.fetch_github(billing_sources.current_period())
    assert result.status == "error"
    assert result.amount_usd is None
    assert result.breakdown[0]["observed_cycle_amount_usd"] == 0.16
    assert "cruza meses calendario" in result.detail


def test_github_acepta_ciclo_pago_alineado_al_mes(monkeypatch):
    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "included_minutes": 100,
                "total_minutes_used": 120,
                "total_paid_minutes_used": 20,
            }

    monkeypatch.setenv("GITHUB_BILLING_TOKEN", "ghp_fake")
    monkeypatch.setenv("GITHUB_BILLING_USER", "alguien")
    monkeypatch.setenv("GITHUB_BILLING_CYCLE_DAY", "1")
    monkeypatch.setattr(billing_sources.requests, "get", lambda *a, **k: _Resp())

    result = billing_sources.fetch_github(billing_sources.current_period())
    assert result.status == "ok"
    assert result.amount_usd == 0.16


def test_github_cero_tambien_exige_ciclo_alineado_al_mes(monkeypatch):
    """$0 del ciclo 15→15 no demuestra que todo el mes haya sido gratis."""
    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "included_minutes": 100,
                "total_minutes_used": 0,
                "total_paid_minutes_used": 0,
            }

    monkeypatch.setenv("GITHUB_BILLING_TOKEN", "ghp_fake")
    monkeypatch.setenv("GITHUB_BILLING_USER", "alguien")
    monkeypatch.setenv("GITHUB_BILLING_CYCLE_DAY", "15")
    monkeypatch.setattr(billing_sources.requests, "get", lambda *a, **k: _Resp())

    result = billing_sources.fetch_github(billing_sources.current_period())
    assert result.status == "error"
    assert result.amount_usd is None
    assert result.breakdown[0]["observed_cycle_amount_usd"] == 0.0
    assert "cruza meses calendario" in result.detail


def test_github_checkpoint_temprano_no_se_vuelve_final_por_esperar():
    """Sólo una captura en el borde del mes puede cerrar el ciclo alineado."""
    from datetime import datetime, timezone

    assert billing_sources.snapshot_is_final(
        "2020-10", "github",
        datetime(2020, 10, 15, tzinfo=timezone.utc),
    ) is False
    assert billing_sources.snapshot_is_final(
        "2020-10", "github",
        datetime(2020, 10, 31, 23, 59, 30, tzinfo=timezone.utc),
    ) is True
