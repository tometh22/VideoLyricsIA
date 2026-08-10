"""Admin endpoints for real invoiced cost and model-vs-invoice reconciliation.

Contract these lock down:

* Cost endpoints are admin-only (they expose margin).
* A month with no snapshot must say so rather than reporting $0 — the
  whole point of this feature is that "we have no data" and "it was free"
  stopped being the same answer.
* `cost_per_delivered` divides by videos DELIVERED. The old internal doc
  divided $199.53 of jun-2026 Google Cloud by 173 created jobs and got
  $1.15/video; the honest number over the 65 that shipped is $3.07.
"""

from tests.conftest import auth


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def test_cost_endpoints_require_admin(client, user_token):
    for path in ("/admin/cost/real", "/admin/cost/unit-economics",
                 "/admin/cost/reconcile"):
        res = client.get(path, headers=auth(user_token))
        assert res.status_code == 403, path


def test_cost_refresh_requires_admin(client, user_token):
    res = client.post("/admin/cost/refresh", headers=auth(user_token))
    assert res.status_code == 403


def test_calibracion_de_mes_abierto_es_provisional_y_no_se_guarda(
    client, admin_token, db, monkeypatch,
):
    from contextlib import contextmanager
    import billing_sources
    import database
    import rate_calibration

    @contextmanager
    def _peer():
        yield db

    monkeypatch.setattr(billing_sources, "current_period", lambda: "2099-12")
    monkeypatch.setattr(
        billing_sources,
        "fetch_gcp",
        lambda _period: billing_sources.SourceCost(
            "gcp", "2099-12", amount_usd=50.0,
            breakdown=[{"service": "Vertex AI", "sku": "Veo", "cost": 50.0}],
        ),
    )
    monkeypatch.setattr(database, "scoped_peer_db", _peer)
    monkeypatch.setattr(
        rate_calibration,
        "derive_rates",
        lambda *_args, **_kwargs: {
            "period": "2099-12", "rates": [], "applied": {"veo": 0.5},
        },
    )
    monkeypatch.setattr(
        rate_calibration,
        "store_rates",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("an open-month rate must not be stored")
        ),
    )

    response = client.post(
        "/admin/cost/calibrate-rates", headers=auth(admin_token),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["provisional"] is True
    assert body["stored"] is False
    assert body["calibrated"] is False
    assert body["applied"] == {}
    assert body["provisional_applied"] == {"veo": 0.5}


# ---------------------------------------------------------------------------
# /admin/cost/real
# ---------------------------------------------------------------------------

def test_cost_real_without_snapshot_reports_incomplete(client, admin_token):
    """No snapshot must not read as $0 with a confident total."""
    res = client.get("/admin/cost/real?period=2019-01",
                     headers=auth(admin_token))
    assert res.status_code == 200
    body = res.json()
    assert body["complete"] is False
    assert body["total_usd"] == 0.0
    assert body["not_configured"]          # every source listed as missing
    assert "refresh" in (body.get("detail") or "")


def test_cost_real_rejects_malformed_period(client, admin_token):
    res = client.get("/admin/cost/real?period=2026-13",
                     headers=auth(admin_token))
    assert res.status_code == 400


def test_cost_real_roundtrips_a_snapshot(client, admin_token, db):
    """A persisted snapshot is summed and reported as the source of truth."""
    from database import CostSnapshot

    db.query(CostSnapshot).filter(CostSnapshot.period == "2020-05").delete()
    db.add(CostSnapshot(period="2020-05", source="gcp", amount_usd=199.53,
                        status="ok", is_estimate=False))
    db.add(CostSnapshot(period="2020-05", source="railway", amount_usd=124.54,
                        status="ok", is_estimate=True))
    # A source that was never configured must NOT contribute 0 silently.
    db.add(CostSnapshot(period="2020-05", source="r2", amount_usd=None,
                        status="not_configured"))
    db.commit()

    try:
        res = client.get("/admin/cost/real?period=2020-05",
                         headers=auth(admin_token))
        assert res.status_code == 200
        body = res.json()
        assert body["total_usd"] == 324.07
        assert body["source_of_truth"] == "snapshot"
        assert "r2" in body["not_configured"]
        assert body["complete"] is False        # r2 missing
        # Sorted by spend, biggest first.
        assert body["sources"][0]["source"] == "gcp"
    finally:
        db.query(CostSnapshot).filter(CostSnapshot.period == "2020-05").delete()
        db.commit()


# ---------------------------------------------------------------------------
# /admin/cost/unit-economics — the denominator contract
# ---------------------------------------------------------------------------

def test_unit_economics_divides_by_delivered_not_created(client, admin_token, db):
    """The core defect the 2026-08 audit found, locked down.

    3 jobs created, 1 delivered, $30 invoiced. Cost per delivered video is
    $30 — not $10. The discarded previews cost real money.
    """
    from database import CostSnapshot, Job
    from datetime import datetime, timezone

    period = "2020-06"
    when = datetime(2020, 6, 15, tzinfo=timezone.utc)
    db.query(CostSnapshot).filter(CostSnapshot.period == period).delete()
    db.query(Job).filter(Job.tenant_id == "unit-econ-test").delete()

    db.add(CostSnapshot(period=period, source="gcp", amount_usd=30.0,
                        status="ok"))
    for jid, status in (("ue1", "done"), ("ue2", "bg_preview_done"),
                        ("ue3", "rejected")):
        db.add(Job(job_id=jid, user_id=1, tenant_id="unit-econ-test",
                   artist="A", filename="a.mp3", status=status,
                   created_at=when))
    db.commit()

    try:
        res = client.get(
            f"/admin/cost/unit-economics?period={period}&price_per_video_usd=13.5",
            headers=auth(admin_token))
        assert res.status_code == 200
        body = res.json()

        assert body["videos_created"] == 3
        assert body["videos_delivered"] == 1
        assert body["cost_per_delivered"] == 30.0
        # Kept only to show the operator how flattering the wrong one is.
        assert body["cost_per_created_MISLEADING"] == 10.0
        # $13.50 revenue against $30 cost is deeply negative — the point.
        assert body["margin_per_video"] == -16.5
        assert body["cost_complete"] is False   # only gcp snapshotted
        assert "railway" in body["missing_sources"]
    finally:
        db.query(CostSnapshot).filter(CostSnapshot.period == period).delete()
        db.query(Job).filter(Job.tenant_id == "unit-econ-test").delete()
        db.commit()


def test_unit_economics_handles_month_with_no_videos(client, admin_token):
    res = client.get("/admin/cost/unit-economics?period=2019-02",
                     headers=auth(admin_token))
    assert res.status_code == 200
    body = res.json()
    assert body["videos_delivered"] == 0
    assert body["cost_per_delivered"] is None
    assert body["margin_per_video"] is None


# ---------------------------------------------------------------------------
# /admin/cost/reconcile
# ---------------------------------------------------------------------------

def test_reconcile_reports_calibration_factor(client, admin_token):
    res = client.get("/admin/cost/reconcile?period=2019-03",
                     headers=auth(admin_token))
    assert res.status_code == 200
    body = res.json()
    # No invoices snapshotted for that month → everything AI is missing.
    assert set(body["invoiced_sources_missing"]) == {"gcp", "openai", "replicate"}
    assert body["invoiced_usd"] == 0.0
    # The modeled side must cover the SAME calendar month as the invoice.
    # It used to call cost_dashboard_global(since_days=N), which measures a
    # window ending today — so asking about June in August compared two
    # disjoint months and then told the operator to recalibrate the rate
    # table from the result.
    assert body["modeled_usd"] == 0.0, "2019-03 no tiene provenance"
    assert "counted_environments" in body
    assert "by_tool_modeled" in body


def test_reconcile_rejects_malformed_period(client, admin_token):
    res = client.get("/admin/cost/reconcile?period=nope",
                     headers=auth(admin_token))
    assert res.status_code == 400


# ---------------------------------------------------------------------------
# /admin/cost/umg + /admin/cost/business — cross-environment attribution
# ---------------------------------------------------------------------------

def test_attribution_endpoints_require_admin(client, user_token):
    for path in ("/admin/cost/umg", "/admin/cost/business"):
        assert client.get(path, headers=auth(user_token)).status_code == 403


def test_umg_endpoint_warns_when_peer_env_missing(client, admin_token):
    """Managed production for UMG runs in STAGING. Answering from a single
    environment is legitimate but partial, and the response has to say so —
    silently reporting half the cost is the failure mode that started this
    whole audit."""
    res = client.get("/admin/cost/umg", headers=auth(admin_token))
    assert res.status_code == 200
    body = res.json()
    assert body["single_environment"] is True
    assert "STAGING" in body["warning"]
    assert body["peer_configured"] is False


def test_umg_endpoint_rejects_malformed_period(client, admin_token):
    res = client.get("/admin/cost/umg?period=2026-99",
                     headers=auth(admin_token))
    assert res.status_code == 400


def test_umg_endpoint_reports_missing_invoices_instead_of_zero(client, admin_token):
    """Level 2 needs the real bill. With no snapshot it must decline to
    compute rather than prorate an invented total."""
    res = client.get("/admin/cost/umg?period=2019-04",
                     headers=auth(admin_token))
    assert res.status_code == 200
    body = res.json()
    assert "umg_total" not in body
    assert "refresh" in body["umg_total_unavailable"]


def test_business_endpoint_returns_categories(client, admin_token):
    res = client.get("/admin/cost/business", headers=auth(admin_token))
    assert res.status_code == 200
    body = res.json()
    assert "by_category" in body["business"]
    assert "total_direct_cost" in body["business"]
    # The per-song detail belongs to /cost/umg, not here.
    assert "by_song" not in body["umg"]


def test_umg_endpoint_truncates_song_detail(client, admin_token):
    res = client.get("/admin/cost/umg?top=1", headers=auth(admin_token))
    assert res.status_code == 200
    body = res.json()
    assert len(body["umg"]["by_song"]) <= 1
    assert "by_song_truncated" in body["umg"]


# ---------------------------------------------------------------------------
# Pool hygiene
# ---------------------------------------------------------------------------

def test_cost_endpoints_do_not_leak_db_sessions(client, admin_token):
    """These endpoints open sessions the framework does NOT manage — the
    peer environment and the deliveries portal — so nothing returns them
    automatically.

    Without `DELIVERIES_DATABASE_URL`, `DeliveriesSessionLocal` *is*
    `SessionLocal`, so a leak here drains the MAIN pool. And `pool_stats()`
    reports the whole pool, so one stranded session fails health checks and
    unrelated leak tests, pointing the blame anywhere but here. Hence the
    `scoped_*` context managers, and hence this test.
    """
    from database import pool_stats

    before = pool_stats().get("checked_out", 0)
    for path in ("/admin/cost/umg", "/admin/cost/business",
                 "/admin/quality/change-requests",
                 "/admin/cost/unit-economics?period=2019-05",
                 "/admin/cost/reconcile?period=2019-05"):
        assert client.get(path, headers=auth(admin_token)).status_code == 200, path
    # SQLite returns {} from pool_stats — the assertion is a no-op there but
    # real in CI, which runs Postgres.
    assert pool_stats().get("checked_out", 0) == before


def test_change_requests_endpoint_requires_admin(client, user_token):
    res = client.get("/admin/quality/change-requests", headers=auth(user_token))
    assert res.status_code == 403


def test_change_requests_endpoint_shape(client, admin_token):
    res = client.get("/admin/quality/change-requests", headers=auth(admin_token))
    assert res.status_code == 200
    body = res.json()
    for key in ("requests", "categories", "requests_per_delivery",
                "excluded_as_noise", "unclassified"):
        assert key in body
    keys = {c["key"] for c in body["categories"]}
    assert "puntos_finales" in keys


def test_change_requests_rejects_malformed_period(client, admin_token):
    res = client.get("/admin/quality/change-requests?period=2026-13",
                     headers=auth(admin_token))
    assert res.status_code == 400
