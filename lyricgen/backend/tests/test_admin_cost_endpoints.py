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

import os
from pathlib import Path
import subprocess
import sys

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


def test_cost_real_live_del_mes_abierto_sigue_provisional(
    client, admin_token, monkeypatch,
):
    """Cobertura sana no convierte un acumulado intrames en factura final."""
    import billing_sources

    period = "2099-12"
    monkeypatch.setattr(billing_sources, "current_period", lambda: period)
    monkeypatch.setattr(billing_sources, "fetch_all", lambda **kwargs: {
        "period": period,
        "total_usd": 7.0,
        "sources": [],
        "configured": list(billing_sources.SOURCES),
        "not_configured": [],
        "errored": [],
        "complete": True,
    })

    res = client.get(
        f"/admin/cost/real?period={period}&live=true",
        headers=auth(admin_token),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["total_usd"] == 7.0
    assert body["complete"] is False
    assert body["provisional"] is True
    assert body["source_of_truth"] == "live"
    assert "período abierto" in body["detail"]


def test_cost_refresh_del_mes_abierto_sigue_provisional(
    client, admin_token, db, monkeypatch,
):
    """Persistir checkpoints sanos no cierra una factura que sigue creciendo."""
    import billing_sources
    from database import CostSnapshot

    period = "2099-12"
    monkeypatch.setattr(billing_sources, "current_period", lambda: period)
    monkeypatch.setattr(billing_sources, "fetch_all", lambda **kwargs: {
        "period": period,
        "total_usd": float(len(billing_sources.SOURCES)),
        "sources": [
            {
                "source": source, "period": period, "amount_usd": 1.0,
                "status": "ok", "detail": "month-to-date",
                "is_estimate": False, "breakdown": [],
            }
            for source in billing_sources.SOURCES
        ],
    })
    db.query(CostSnapshot).filter(CostSnapshot.period == period).delete()
    db.commit()

    try:
        res = client.post(
            f"/admin/cost/refresh?period={period}",
            headers=auth(admin_token),
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["total_usd"] == float(len(billing_sources.SOURCES))
        assert body["complete"] is False
        assert body["provisional"] is True
        assert "período abierto" in body["detail"]
        assert db.query(CostSnapshot).filter(
            CostSnapshot.period == period,
        ).count() == len(billing_sources.SOURCES)
    finally:
        db.query(CostSnapshot).filter(CostSnapshot.period == period).delete()
        db.commit()


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


def test_unit_economics_no_trata_ok_sin_monto_como_completo(
    client, admin_token, db,
):
    import billing_sources
    from database import CostSnapshot

    period = "2019-04"
    db.query(CostSnapshot).filter(CostSnapshot.period == period).delete()
    for source in billing_sources.SOURCES:
        db.add(CostSnapshot(
            period=period, source=source, amount_usd=None, status="ok",
        ))
    db.commit()
    try:
        res = client.get(
            f"/admin/cost/unit-economics?period={period}",
            headers=auth(admin_token),
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["cost_complete"] is False
        assert body["missing_sources"] == sorted(billing_sources.SOURCES)
        assert body["real_cost_usd"] == 0.0
    finally:
        db.query(CostSnapshot).filter(CostSnapshot.period == period).delete()
        db.commit()


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


def test_reconcile_trata_ok_sin_monto_como_fuente_faltante(
    client, admin_token, db,
):
    from database import CostSnapshot

    period = "2019-05"
    sources = ("gcp", "openai", "replicate")
    db.query(CostSnapshot).filter(CostSnapshot.period == period).delete()
    for source in sources:
        db.add(CostSnapshot(
            period=period, source=source, amount_usd=None, status="ok",
        ))
    db.commit()
    try:
        res = client.get(
            f"/admin/cost/reconcile?period={period}",
            headers=auth(admin_token),
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert set(body["invoiced_sources_missing"]) == set(sources)
        assert body["invoiced_usd"] == 0.0
    finally:
        db.query(CostSnapshot).filter(CostSnapshot.period == period).delete()
        db.commit()


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


def test_cost_reports_run_blocking_queries_in_threadpool():
    """Plain ``def`` makes FastAPI keep remote SQLAlchemy I/O off the loop."""
    import inspect
    from admin import (
        admin_cost_dashboard,
        admin_cost_business,
        admin_margin_dashboard,
        admin_cost_rates,
        admin_cost_reconcile,
        admin_tenant_cost,
        admin_cost_umg,
        admin_cost_unit_economics,
        admin_change_requests,
    )

    assert not inspect.iscoroutinefunction(admin_cost_umg)
    assert not inspect.iscoroutinefunction(admin_cost_dashboard)
    assert not inspect.iscoroutinefunction(admin_margin_dashboard)
    assert not inspect.iscoroutinefunction(admin_cost_business)
    assert not inspect.iscoroutinefunction(admin_cost_unit_economics)
    assert not inspect.iscoroutinefunction(admin_cost_reconcile)
    assert not inspect.iscoroutinefunction(admin_change_requests)
    assert not inspect.iscoroutinefunction(admin_cost_rates)
    assert not inspect.iscoroutinefunction(admin_tenant_cost)


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

def test_peer_reuses_deliveries_pool_when_urls_match():
    """Staging uses the production DB for both deliveries and attribution.
    Import in a subprocess so the environment-specific engines cannot leak
    into the test suite's already-imported database module."""
    env = os.environ.copy()
    env["DATABASE_URL"] = "postgresql://local:local@127.0.0.1/local"
    env["DELIVERIES_DATABASE_URL"] = (
        "postgresql://peer:peer@127.0.0.1/production"
    )
    env.pop("PEER_DATABASE_URL", None)
    backend_dir = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import database; "
            "assert database.peer_engine is database.deliveries_engine; "
            "assert database.PeerSessionLocal is database.DeliveriesSessionLocal",
        ],
        cwd=backend_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr

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


def test_unit_economics_refuses_to_divide_with_no_snapshot_at_all(
    client, admin_token, db,
):
    """Con `cost_snapshots` vacía este endpoint devolvía margen 100%.

    `real_total` = 0 → `cost_per_delivered` = 0.0 → `margin_pct` = 1.0, o
    sea el precio entero como ganancia. El `cost_complete: false` viajaba
    en un campo aparte que nadie mira, y la tabla está vacía en las DOS
    bases porque /cost/refresh nunca se corrió.

    Etiquetar no alcanza cuando el número etiquetado es justo el que
    alguien copia para cotizarle a un cliente: un `null` obliga a mirar por
    qué, un `0.0` con nota al pie se lee como una buena noticia.
    """
    from datetime import datetime, timezone

    from database import CostSnapshot, Job

    # Tiene que haber ENTREGAS: si el denominador fuera 0 el endpoint
    # devolvería null por otro motivo y el test pasaría sin ejercitar nada.
    period = "2021-07"
    when = datetime(2021, 7, 10, tzinfo=timezone.utc)
    db.query(CostSnapshot).filter(CostSnapshot.period == period).delete()
    db.query(Job).filter(Job.tenant_id == "sin-snapshot-test").delete()
    for jid in ("ns1", "ns2"):
        db.add(Job(job_id=jid, user_id=1, tenant_id="sin-snapshot-test",
                   artist="A", filename="a.mp3", status="done", created_at=when))
    db.commit()
    try:
        r = client.get(
            f"/admin/cost/unit-economics?period={period}&price_per_video_usd=13.5",
            headers=auth(admin_token))
        assert r.status_code == 200
        d = r.json()
        assert d["videos_delivered"] >= 2, "sin denominador el test no prueba nada"
        assert d["real_cost_usd"] == 0
        assert d["cost_per_delivered"] is None, "0/N daría margen 100%"
        assert d["cost_per_created_MISLEADING"] is None
        assert d["margin_per_video"] is None
        assert d["margin_pct"] is None
        assert d["missing_sources"], "tiene que decir QUÉ falta"
    finally:
        db.query(Job).filter(Job.tenant_id == "sin-snapshot-test").delete()
        db.commit()


# ---------------------------------------------------------------------------
# Los CUATRO denominadores
# ---------------------------------------------------------------------------
#
# `videos_delivered` no es el costo por video. Medido en ago-2026 sobre las
# bases de prod y staging: 163 entregados, 77 de cliente (47%), y la unidad
# facturable no es el job sino la CANCIÓN — una canción entregada arrastra
# ~2,87 jobs entre variantes, re-renders y ediciones.
#
# Dividir la factura por el denominador equivocado da hasta 2,8x de
# diferencia, siempre hacia abajo. Y ese es el número que se copia para
# cotizarle a un sello.

def test_unit_economics_separa_jobs_de_ci_de_los_de_cliente(
    client, admin_token, db,
):
    """5 jobs entregados: 2 de cliente (1 sola canción) y 3 que no facturan."""
    from database import CostSnapshot, Job
    from datetime import datetime, timezone

    period = "2020-07"
    when = datetime(2020, 7, 15, tzinfo=timezone.utc)
    tenants = ("den-cliente", "preflight_staging", "mx_a_1780448038", "genly")
    db.query(CostSnapshot).filter(CostSnapshot.period == period).delete()
    db.query(Job).filter(Job.tenant_id.in_(tenants)).delete(
        synchronize_session=False)

    db.add(CostSnapshot(period=period, source="gcp", amount_usd=100.0,
                        status="ok"))
    filas = [
        # Dos jobs del MISMO tema: una variante y su re-render. Es UNA
        # canción entregada, no dos.
        ("d1", "den-cliente", "Los Bunkers", "Nada Nuevo Bajo El Sol"),
        ("d2", "den-cliente", "los bunkers", "nada nuevo bajo el sol"),
        # Barrido de CI: el patrón viejo lo contaba como cliente.
        ("d3", "preflight_staging", "CI", "smoke"),
        ("d4", "mx_a_1780448038", "CI", "matriz"),
        # Cuenta del equipo con una canción que NO está en el portal: I+D.
        ("d5", "genly", "Demo", "interno"),
    ]
    for jid, tenant, artist, song in filas:
        db.add(Job(job_id=jid, user_id=1, tenant_id=tenant, artist=artist,
                   song_title=song, filename="a.mp3", status="done",
                   created_at=when, completed_at=when))
    db.commit()

    try:
        res = client.get(f"/admin/cost/unit-economics?period={period}",
                         headers=auth(admin_token))
        assert res.status_code == 200
        body = res.json()

        assert body["videos_delivered"] == 5,   "el crudo cuenta todo"
        assert body["delivered_client_jobs"] == 2, "sin CI ni I+D interno"
        assert body["delivered_client_songs"] == 1, "las 2 filas son un tema"

        # El costo real por canción entregada es 5x el que sugiere el crudo.
        # Ese 5x es la diferencia entre cotizar con margen y cotizar a pérdida.
        assert body["cost_per_delivered"] == 20.0
        assert body["cost_per_client_song"] == 100.0
    finally:
        db.query(CostSnapshot).filter(CostSnapshot.period == period).delete()
        db.query(Job).filter(Job.tenant_id.in_(tenants)).delete(
            synchronize_session=False)
        db.commit()


def test_unit_economics_cuenta_la_produccion_gestionada_de_umg(
    client, admin_token, db,
):
    """El bug que el filtro por tenant introducía, a nivel endpoint.

    Las entregas del portal corren bajo cuentas del EQUIPO. Descartarlas
    como I+D interno dejaba 23 canciones de las ≥77 realmente entregadas en
    ago-2026: el KPI que la pantalla rotula "el correcto" salía 3,35x
    arriba del costo real. Y el error crece mes a mes, porque la producción
    gestionada se fue corriendo cada vez más a esas cuentas.
    """
    from database import CostSnapshot, Delivery, Job
    from datetime import datetime, timezone

    period = "2020-08"
    when = datetime(2020, 8, 10, tzinfo=timezone.utc)
    tenants = ("agus77", "default")
    db.query(CostSnapshot).filter(CostSnapshot.period == period).delete()
    db.query(Job).filter(Job.tenant_id.in_(tenants)).delete(
        synchronize_session=False)
    db.query(Delivery).filter(Delivery.job_id.in_(("ug1", "ug2"))).delete(
        synchronize_session=False)

    db.add(CostSnapshot(period=period, source="gcp", amount_usd=200.0,
                        status="ok"))
    entregas = [
        ("ug1", "agus77", "Los Bunkers", "Nada Nuevo Bajo El Sol"),
        ("ug2", "default", "Coti", "Nada Fue Un Error"),
    ]
    for jid, tenant, artist, song in entregas:
        db.add(Job(job_id=jid, user_id=1, tenant_id=tenant, artist=artist,
                   song_title=song, filename="a.mp3", status="done",
                   created_at=when, completed_at=when))
        # El portal es lo que prueba que se entregó y se cobró.
        db.add(Delivery(job_id=jid, tenant_snapshot="umusic",
                        added_by_user_id=1,
                        artist_snapshot=artist, song_title_snapshot=song))
    # Mismas cuentas, canción que NO está en el portal: sigue siendo I+D.
    db.add(Job(job_id="ug3", user_id=1, tenant_id="agus77", artist="Prueba",
               song_title="Experimento", filename="a.mp3", status="done",
               created_at=when, completed_at=when))
    db.commit()

    try:
        res = client.get(f"/admin/cost/unit-economics?period={period}",
                         headers=auth(admin_token))
        assert res.status_code == 200
        body = res.json()

        assert body["videos_delivered"] == 3
        assert body["delivered_client_jobs"] == 2, "las 2 del portal"
        assert body["delivered_client_songs"] == 2
        # $200 / 2 = $100. Descartando el portal habría dado 0 canciones y
        # el KPI entero en null.
        assert body["cost_per_client_song"] == 100.0
        assert body["portal_disponible"] is True
        # Y el margen sale del MISMO denominador que el KPI de la pantalla.
        assert body["margin_per_video"] == round(13.5 - 100.0, 4)
    finally:
        db.query(Delivery).filter(Delivery.job_id.in_(("ug1", "ug2"))).delete(
            synchronize_session=False)
        db.query(CostSnapshot).filter(CostSnapshot.period == period).delete()
        db.query(Job).filter(Job.tenant_id.in_(tenants)).delete(
            synchronize_session=False)
        db.commit()



# ---------------------------------------------------------------------------
# /admin/margin — ventana explícita
# ---------------------------------------------------------------------------
#
# `since_days` cuenta hacia atrás desde HOY, así que no puede alinearse con
# un mes calendario. En la página de costos consolidada eso ponía la
# atribución por tenant de "los últimos 30 días" al lado del gasto FACTURADO
# de julio, sin decir en ningún lado que eran períodos distintos.

def test_margin_respeta_la_ventana_explicita(client, admin_token, db):
    from database import Job
    from datetime import datetime, timezone

    tenant = "margin-window-test"
    db.query(Job).filter(Job.tenant_id == tenant).delete()
    # Uno DENTRO de julio y uno en agosto. Con `until` sólo cuenta el
    # primero; sin `until` (o con since_days) entrarían los dos.
    for jid, cuando in (
        ("mw-jul", datetime(2021, 7, 15, tzinfo=timezone.utc)),
        ("mw-ago", datetime(2021, 8, 15, tzinfo=timezone.utc)),
    ):
        db.add(Job(job_id=jid, user_id=1, tenant_id=tenant, artist="A",
                   filename="a.mp3", status="done", created_at=cuando,
                   completed_at=cuando))
    db.commit()

    def _de_este_tenant(body):
        return next((t for t in body["by_tenant"] if t["tenant_id"] == tenant),
                    None)

    try:
        julio = client.get(
            "/admin/margin?since=2021-07-01&until=2021-07-31",
            headers=auth(admin_token))
        assert julio.status_code == 200
        fila = _de_este_tenant(julio.json())
        assert fila is not None and fila["done"] == 1, "sólo el job de julio"

        # `until` es INCLUSIVE: el último día del mes tiene que entrar.
        ultimo = client.get(
            "/admin/margin?since=2021-07-15&until=2021-07-15",
            headers=auth(admin_token))
        assert _de_este_tenant(ultimo.json())["done"] == 1

        ambos = client.get(
            "/admin/margin?since=2021-07-01&until=2021-08-31",
            headers=auth(admin_token))
        assert _de_este_tenant(ambos.json())["done"] == 2
    finally:
        db.query(Job).filter(Job.tenant_id == tenant).delete()
        db.commit()


def test_margin_rechaza_una_ventana_al_reves(client, admin_token):
    res = client.get("/admin/margin?since=2021-07-31&until=2021-07-01",
                     headers=auth(admin_token))
    assert res.status_code == 400

    malo = client.get("/admin/margin?since=julio", headers=auth(admin_token))
    assert malo.status_code == 400


def test_margin_sin_ventana_se_comporta_como_siempre(client, admin_token):
    # El contrato viejo (`since_days`) no se toca: hay llamadores que lo usan
    # y una regresión ahí es silenciosa.
    res = client.get("/admin/margin?since_days=30", headers=auth(admin_token))
    assert res.status_code == 200
    assert res.json()["since_days"] == 30
