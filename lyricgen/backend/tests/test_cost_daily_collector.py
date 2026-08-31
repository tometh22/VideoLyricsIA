"""Tests for the daily cost collector.

Every test here pins a bug that was found by adversarial review of the
design, before any of this shipped. The names say which one.

The headline: `billing_sources.fetch_railway` converts unit-minutes to
unit-MONTHS by dividing by the length of the requested window, so a one-day
window turns 8 GB of resident memory into "8 GB-months" = $80 for one day.
Measured against the real project, seven one-day calls summed to $613.51
where the whole month is $101.14 — 26.9x. If someone ever "simplifies"
`_railway_day` to call `fetch_railway` with a one-day period, these tests
go red.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

import cost_daily_collector as cdc
from cost_daily_collector import (
    DayResult,
    _check_dims_sum_to_total,
    _month_minutes,
    _persist,
    collect_day,
    pending_gaps,
    run_backfill,
)
from database import CostCollectionRun, CostDaily


# ---------------------------------------------------------------------------
# El bug de 26,9x
# ---------------------------------------------------------------------------

def test_month_minutes_uses_the_month_not_the_window():
    """31 días = 44.640 min; febrero no bisiesto = 40.320."""
    assert _month_minutes(date(2026, 8, 15)) == 31 * 24 * 60
    assert _month_minutes(date(2026, 2, 10)) == 28 * 24 * 60
    assert _month_minutes(date(2028, 2, 10)) == 29 * 24 * 60   # bisiesto


def test_railway_daily_sums_to_the_month_not_31x_it(monkeypatch):
    """Σ(31 días) tiene que dar el mes, no 31 veces el mes.

    Este es EL test del módulo. Con la fórmula de `fetch_railway` (dividir
    por los minutos de la ventana), 8 GB residentes darían $10 por día y
    $310 en el mes, cuando el mes real son $10.
    """
    RESIDENTES_GB = 8.0
    monkeypatch.setenv("RAILWAY_API_TOKEN", "t")
    monkeypatch.setenv("RAILWAY_PROJECT_ID", "p")

    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            # 8 GB constantes durante un día = 8 × 1440 GB-min
            return {"data": {"usage": [
                {"measurement": "MEMORY_USAGE_GB", "value": RESIDENTES_GB * 1440,
                 "tags": {"serviceId": "svc"}},
            ]}}

    monkeypatch.setattr(cdc.__dict__["bs"], "HTTP_TIMEOUT", 5, raising=False)
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp())

    total = 0.0
    for d in range(1, 32):
        res = cdc._railway_day(date(2026, 8, d))
        assert res.status == "ok"
        total += next(r["amount_usd"] for r in res.rows if r["dim_type"] == "total")

    # 8 GB residentes todo el mes × $10/GB-mes = $80. Ni $2.480 ni $2,58.
    assert total == pytest.approx(RESIDENTES_GB * 10.0, rel=1e-6)


def test_railway_empty_window_is_error_not_zero(monkeypatch):
    """Una ventana vacía no se puede distinguir de un cero real.

    Railway nunca factura cero por un proyecto corriendo, así que un cero
    'exitoso' pisaría un valor bueno en el re-collect.
    """
    monkeypatch.setenv("RAILWAY_API_TOKEN", "t")
    monkeypatch.setenv("RAILWAY_PROJECT_ID", "p")

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"data": {"usage": []}}
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp())

    res = cdc._railway_day(date(2026, 8, 5))
    assert res.status == "error"
    assert res.rows == []


# ---------------------------------------------------------------------------
# El borde inclusivo de OpenAI
# ---------------------------------------------------------------------------

def test_openai_day_never_reaches_into_the_next_day(monkeypatch):
    """La API incluye el bucket que ARRANCA en `end_time`.

    Con `end = D+1 00:00` cada día trae también D+1 y la suma del mes sale
    al doble. Medido en la org real: julio dio $567 con el borde inclusivo
    contra $492 con el correcto.
    """
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "sk-admin-x")
    capturado = {}

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"data": [], "has_more": False}

    def _get(url, headers=None, params=None, timeout=None):
        capturado.update(params)
        return _Resp()
    import requests
    monkeypatch.setattr(requests, "get", _get)

    cdc._openai_day(date(2026, 8, 15))
    fin = datetime.fromtimestamp(capturado["end_time"], timezone.utc)
    assert fin.date() == date(2026, 8, 15), "el fin se fue al día siguiente"
    assert (fin.hour, fin.minute, fin.second) == (23, 59, 59)


def test_openai_keeps_every_line_item_so_the_filter_can_change_later(monkeypatch):
    """El filtro de line_items se aplica AL LEER, no al colectar.

    En julio `gpt-4o-mini` no era de Genly; en agosto sí. Si el filtro se
    aplicara al colectar, cambiar de opinión exigiría re-colectar meses que
    las APIs ya no exponen.
    """
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "sk-admin-x")

    class _Resp:
        def raise_for_status(self): pass
        def json(self):
            return {"data": [{"results": [
                {"line_item": "whisper", "amount": {"value": 1.0}},
                {"line_item": "gpt-5.4, output", "amount": {"value": 9.0}},
            ]}], "has_more": False}
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())

    res = cdc._openai_day(date(2026, 8, 15))
    lineas = {r["dim_value"] for r in res.rows if r["dim_type"] == "line_item"}
    assert lineas == {"whisper", "gpt-5.4, output"}
    total = next(r["amount_usd"] for r in res.rows if r["dim_type"] == "total")
    assert total == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# El invariante: las partes suman al total
# ---------------------------------------------------------------------------

def test_dims_that_do_not_sum_to_total_raise():
    rows = [
        cdc._row("total", "total", 10.0),
        cdc._row("sku", "a", 6.0),
        cdc._row("sku", "b", 3.0),      # falta $1
    ]
    with pytest.raises(ValueError, match="suma 9.0000 pero el total es 10.0000"):
        _check_dims_sum_to_total(rows, "gcp", date(2026, 8, 1))


def test_dims_that_sum_to_total_pass():
    rows = [
        cdc._row("total", "total", 10.0),
        cdc._row("sku", "a", 6.0),
        cdc._row("sku", "b", 4.0),
        cdc._row("service", "x", 10.0),   # otra dimensión, mismo total
    ]
    _check_dims_sum_to_total(rows, "gcp", date(2026, 8, 1))


# ---------------------------------------------------------------------------
# Persistencia: DELETE-then-INSERT, no upsert
# ---------------------------------------------------------------------------

def test_reprocessing_a_day_drops_dimensions_that_disappeared(db):
    """Una dimensión que deja de venir tiene que desaparecer.

    Escenario real: el 1-sep se colectan los SKU del 30-ago = {Veo, Imagen}.
    El 2-sep Google reclasifica Imagen a crédito y el día vuelve como {Veo}.
    Con upsert, la fila de Imagen queda con su importe viejo y
    SUM(sku) > total para siempre.
    """
    d = date(2019, 3, 1)
    _persist(db, DayResult("gcp", d, "ok", [
        cdc._row("total", "total", 10.0),
        cdc._row("sku", "Veo", 6.0),
        cdc._row("sku", "Imagen", 4.0),
    ]))
    assert db.query(CostDaily).filter(CostDaily.day == d).count() == 3

    _persist(db, DayResult("gcp", d, "ok", [
        cdc._row("total", "total", 6.0),
        cdc._row("sku", "Veo", 6.0),
    ]))
    filas = db.query(CostDaily).filter(CostDaily.day == d).all()
    assert {f.dim_value for f in filas} == {"total", "Veo"}
    total = next(f.amount_usd for f in filas if f.dim_type == "total")
    skus = sum(f.amount_usd for f in filas if f.dim_type == "sku")
    assert total == pytest.approx(skus)


def test_monthly_grain_is_not_touched_when_a_day_is_reprocessed(db):
    """Un hecho mensual no se puede borrar al re-colectar un día.

    `fixed` vive en grain='month'; si el DELETE no filtrara por grain, cada
    re-colecta diaria borraría las suscripciones del mes.
    """
    d = date(2019, 4, 1)
    _persist(db, DayResult("fixed", d, "ok", [
        cdc._row("total", "total", 24.0, grain="month"),
    ]))
    _persist(db, DayResult("fixed", d, "ok", [
        cdc._row("total", "total", 24.0, grain="month"),
        cdc._row("sku", "vercel_pro", 24.0, grain="month"),
    ]))
    filas = db.query(CostDaily).filter(CostDaily.day == d).all()
    assert all(f.grain == "month" for f in filas)
    assert len(filas) == 2


def test_amount_is_nullable_so_a_failed_source_is_not_free(db):
    """Una fuente que no se pudo consultar no puede leerse como $0."""
    d = date(2019, 5, 1)
    _persist(db, DayResult("gcp", d, "error", [
        cdc._row("total", "total", None, detail="429"),
    ], "rate limited"))
    fila = db.query(CostDaily).filter(CostDaily.day == d).one()
    assert fila.amount_usd is None


# ---------------------------------------------------------------------------
# Estado de la recolección
# ---------------------------------------------------------------------------

def test_a_failed_day_leaves_evidence_not_silence(db):
    """Sin esto, un día que falló se dibuja igual que un día barato."""
    d = date(2019, 6, 1)
    _persist(db, DayResult("openai", d, "error", [], "boom"))
    run = db.get(CostCollectionRun, (d, "openai"))
    assert run.status == "error"
    assert run.last_error == "boom"
    assert run.attempts == 1


def test_collect_day_never_raises_even_if_the_collector_explodes(db, monkeypatch):
    """Una fuente rota no puede tumbar el barrido de las demás."""
    def _boom(day):
        raise RuntimeError("proveedor caído")
    monkeypatch.setitem(cdc.COLLECTORS, "railway", _boom)

    res = collect_day(db, date(2019, 7, 1), "railway")
    assert res.status == "error"
    assert "proveedor caído" in res.detail
    assert db.get(CostCollectionRun, (date(2019, 7, 1), "railway")).status == "error"


# ---------------------------------------------------------------------------
# Backfill guiado por huecos
# ---------------------------------------------------------------------------

def test_gaps_self_heal_after_an_outage_longer_than_the_window(db):
    """Una ventana fija de 3 días perdería los días 4 y 5 para siempre.

    Toda fuente acá tiene ventana móvil en su API, así que un día que no se
    recupera no se recupera nunca.
    """
    hoy = date(2019, 8, 20)
    # El colector estuvo cinco días caído: el último ok es el 14.
    for src in cdc.ALL_SOURCES:
        db.add(CostCollectionRun(day=date(2019, 8, 14), source=src, status="ok"))
    db.commit()

    huecos = pending_gaps(db, today=hoy, days=10)
    dias = {d for d, _ in huecos}
    # ayer = 19; ventana de 10 días = 10..19; el 14 ya está ok
    assert date(2019, 8, 19) in dias
    assert date(2019, 8, 15) in dias, "no se auto-reparó tras el outage"
    assert date(2019, 8, 14) not in dias
    assert date(2019, 8, 20) not in dias, "hoy todavía está acumulando"


def test_gaps_are_empty_when_everything_was_collected(db):
    hoy = date(2019, 9, 10)
    d = hoy - timedelta(days=5)
    while d < hoy:
        for src in cdc.ALL_SOURCES:
            db.add(CostCollectionRun(day=d, source=src, status="ok"))
        d += timedelta(days=1)
    db.commit()
    assert pending_gaps(db, today=hoy, days=5) == []


def test_not_configured_counts_as_resolved_so_it_is_not_retried_forever(db):
    """Una fuente sin credenciales no debe reintentarse en cada corrida."""
    hoy = date(2019, 10, 10)
    for src in cdc.ALL_SOURCES:
        db.add(CostCollectionRun(day=hoy - timedelta(days=1), source=src,
                                 status="not_configured"))
    db.commit()
    assert pending_gaps(db, today=hoy, days=1) == []


def test_backfill_reports_what_failed(db, monkeypatch):
    monkeypatch.setattr(cdc, "COLLECTORS",
                        {"railway": lambda day: DayResult("railway", day, "error", [], "nope")})
    monkeypatch.setattr(cdc, "BATCH_COLLECTORS", {})
    monkeypatch.setattr(cdc, "ALL_SOURCES", ("railway",))
    monkeypatch.setattr(cdc.time, "sleep", lambda *_: None)
    out = run_backfill(db, today=date(2019, 11, 3), days=2)
    assert out["attempted"] == 2
    assert out["error"] == 2
    assert out["errors"][0]["detail"] == "nope"


def test_batch_sources_are_asked_once_per_month_not_once_per_day(db, monkeypatch):
    """Un mes de huecos en GCP no puede costar 31 escaneos de BigQuery.

    La tabla de export está particionada por tiempo de ingesta, así que cada
    query escanea la tabla entera: 30 queries/mes contra una tabla que sólo
    crece es el panel de costos generando costo.
    """
    llamadas = []

    def _fake_gcp(any_day):
        llamadas.append(any_day)
        first = any_day.replace(day=1)
        out, d = [], first
        import calendar as _c
        last = date(first.year, first.month, _c.monthrange(first.year, first.month)[1])
        while d <= last:
            out.append(DayResult("gcp", d, "ok", [cdc._row("total", "total", 1.0)]))
            d += timedelta(days=1)
        return out

    monkeypatch.setattr(cdc, "COLLECTORS", {})
    monkeypatch.setattr(cdc, "BATCH_COLLECTORS", {"gcp": _fake_gcp})
    monkeypatch.setattr(cdc, "ALL_SOURCES", ("gcp",))
    monkeypatch.setattr(cdc.time, "sleep", lambda *_: None)

    # 20 días de huecos, todos dentro del mismo mes
    out = run_backfill(db, today=date(2019, 12, 21), days=20)
    assert out["attempted"] == 20
    assert out["ok"] == 20
    assert len(llamadas) == 1, f"se llamó {len(llamadas)} veces, esperaba 1"
