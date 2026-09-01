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
    _persist,
    collect_day,
    pending_gaps,
    run_backfill,
)
from database import CostCollectionRun, CostDaily


# ---------------------------------------------------------------------------
# El bug de 26,9x
# ---------------------------------------------------------------------------

def test_railway_no_divide_por_ninguna_ventana():
    """La tarifa es por MINUTO, igual que en la factura de Railway.

    Historia de este cálculo, que se equivocó dos veces en la misma línea:

    1. Dividía por los minutos de la VENTANA. En grano diario eso daba
       **26,9x de más**: Σ7 días = $613,51 contra $101,14 del mes.
    2. Pasó a dividir por los minutos del MES. Corregía el 26,9x pero
       dejaba ±3% según el mes tuviera 28, 30 o 31 días.

    Las dos versiones existían para convertir unidad-minuto → unidad-mes, y
    esa conversión nunca hizo falta: la UI de Railway dice "Metrics are
    shown as minutely accumulated values" y su Bill Breakdown cobra
    $0,000231/GB/minuto. Sin división no hay denominador que errar.

    Este test fija esa propiedad: mismo consumo por minuto ⇒ mismo costo,
    caiga en febrero o en un mes de 31 días.
    """
    import billing_sources as bs

    GB_MIN = 1000.0
    esperado = (GB_MIN * bs.RAILWAY_RATES_PER_UNIT_MONTH["MEMORY_USAGE_GB"]
                / bs.RAILWAY_MINUTES_PER_BILLED_MONTH)

    def _corrida(monkeypatch, day):
        monkeypatch.setenv("RAILWAY_API_TOKEN", "t")
        monkeypatch.setenv("RAILWAY_PROJECT_ID", "p")
        cdc._RAILWAY_SERVICE_NAMES.clear()

        class _Resp:
            def raise_for_status(self): pass
            def json(self):
                return {"data": {"usage": [
                    {"measurement": "MEMORY_USAGE_GB", "value": GB_MIN,
                     "tags": {"serviceId": "svc"}}]}}

        import requests
        monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp())
        res = cdc._railway_day(day)
        return next(r["amount_usd"] for r in res.rows if r["dim_type"] == "total")

    with pytest.MonkeyPatch.context() as mp:
        feb = _corrida(mp, date(2026, 2, 10))
    with pytest.MonkeyPatch.context() as mp:
        ago = _corrida(mp, date(2026, 8, 10))

    # `_row` redondea el monto a 6 decimales.
    assert feb == pytest.approx(esperado, abs=1e-6)
    assert ago == pytest.approx(esperado, abs=1e-6)
    assert feb == ago, "el largo del mes no puede cambiar el precio"


def test_tarifas_de_railway_son_las_del_bill_breakdown():
    """Reproducen el "Bill Breakdown" del dashboard, ciclo 20-jul→20-ago-2026.

    No se asertan las tarifas redondeadas que muestra la UI (0,000231) sino
    las LÍNEAS de la factura, que es lo que tiene que cuadrar. La tarifa
    exacta es mensual ÷ 43200 y coincide a 7 cifras.

    Si Railway cambia precios esto falla y avisa, que es mejor que el panel
    valorizando meses enteros a la tarifa vieja en silencio.
    """
    import billing_sources as bs

    # El mes facturable son 30 días fijos, no el mes real. Ésa es la
    # constante que las dos versiones anteriores erraron.
    assert bs.RAILWAY_MINUTES_PER_BILLED_MONTH == 43200
    assert bs.RAILWAY_USD_PER_EGRESS_GB == 0.05

    # La línea de la factura: 440390,97 GB-minuto → $101,9424.
    assert (440390.97 * bs.RAILWAY_RATES_PER_UNIT_MINUTE["MEMORY_USAGE_GB"]
            == pytest.approx(101.9424, abs=0.001))
    # 24435,87 vCPU-minuto → $11,3129.
    assert (24435.87 * bs.RAILWAY_RATES_PER_UNIT_MINUTE["CPU_USAGE"]
            == pytest.approx(11.3129, abs=0.001))
    # 1649,23 GB de egress → $82,4616.
    assert (1649.23 * bs.RAILWAY_USD_PER_EGRESS_GB
            == pytest.approx(82.4616, abs=0.001))


def test_railway_daily_sums_to_the_month_not_31x_it(monkeypatch):
    """Σ(31 días) tiene que dar el mes, no 31 veces el mes.

    Este es EL test del módulo. Dividiendo por los minutos de la VENTANA,
    8 GB residentes daban $10 por día y $310 en el mes.

    Ojo con la expectativa: **no** son $80. Railway cobra contra un mes
    facturable de 30 días fijos, así que 31 días de 8 GB son
    8 × 44640 GB-min × ($10 / 43200) = **$82,67**, un 3,3% más que un mes
    de 30. Eso no es un error de redondeo nuestro: es lo que la factura
    dice. La versión anterior de este test asertaba $80 porque dividía por
    el largo REAL del mes, y con esa expectativa el test habría rechazado
    el cálculo correcto.
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

    import billing_sources as bs
    esperado = (RESIDENTES_GB * 1440 * 31
                * bs.RAILWAY_RATES_PER_UNIT_MONTH["MEMORY_USAGE_GB"]
                / bs.RAILWAY_MINUTES_PER_BILLED_MONTH)
    assert esperado == pytest.approx(82.67, abs=0.01)
    # Ni $2.480 (dividir por la ventana) ni $2,58 (dividir de más).
    assert total == pytest.approx(esperado, rel=1e-6)


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

def test_openai_pide_el_bucket_completo_del_dia(monkeypatch):
    """La ventana tiene que llegar a `D+1 00:00`, no a `D 23:59:59`.

    La API recorta a buckets COMPLETOS. El bucket diario va de `D 00:00` a
    `D+1 00:00`; con la ventana terminando en `23:59:59` no entra entero y
    devuelve **cero buckets**. Verificado contra la organización real el
    20-ago-2026: `23:59:59` → sin buckets ($0); `D+1 00:00` → un bucket de
    $0,1138.

    Ese `23:59:59` venía de arreglar el problema opuesto y se pasó de largo:
    el colector guardó $0,00 TODOS los días de julio y agosto contra una
    factura de $19,41. El test anterior fijaba justamente el borde
    equivocado, así que el bug estaba protegido por su propio test.
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
    ini = datetime.fromtimestamp(capturado["start_time"], timezone.utc)
    fin = datetime.fromtimestamp(capturado["end_time"], timezone.utc)
    assert ini == datetime(2026, 8, 15, tzinfo=timezone.utc)
    # Exactamente 24 h: el bucket del día entra entero y ninguno más.
    assert fin == datetime(2026, 8, 16, tzinfo=timezone.utc)


def test_openai_descarta_el_bucket_del_dia_siguiente(monkeypatch):
    """Con la ventana llegando a `D+1 00:00`, la API puede devolver también
    el bucket que ARRANCA ahí. Sumar días duplicaría.

    El no-doble-conteo se garantiza filtrando por el arranque del bucket, no
    achicando la ventana — así no depende de cómo la API interprete el borde.
    """
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "sk-admin-x")
    d15 = int(datetime(2026, 8, 15, tzinfo=timezone.utc).timestamp())
    d16 = int(datetime(2026, 8, 16, tzinfo=timezone.utc).timestamp())

    class _Resp:
        def raise_for_status(self): pass
        def json(self):
            return {"data": [
                {"start_time": d15, "end_time": d16,
                 "results": [{"line_item": "whisper", "amount": {"value": 3.0}}]},
                # El intruso: arranca justo en el borde.
                {"start_time": d16, "end_time": d16 + 86400,
                 "results": [{"line_item": "whisper", "amount": {"value": 99.0}}]},
            ], "has_more": False}

    import requests
    monkeypatch.setattr(requests, "get",
                        lambda *a, **k: _Resp())

    res = cdc._openai_day(date(2026, 8, 15))
    total = next(r["amount_usd"] for r in res.rows if r["dim_type"] == "total")
    assert total == 3.0, f"se coló el bucket del 16: {total}"


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


# ---------------------------------------------------------------------------
# Comportamiento de costo: no todo lo de un proveedor se comporta igual
# ---------------------------------------------------------------------------

def test_gcp_storage_is_stock_not_variable():
    """GCP no es una sola cosa.

    Marcar el 100% de GCP como 'variable' hacía que el costo marginal de un
    video más se viera más caro de lo que es: Cloud Storage es consecuencia
    acumulada de entregas pasadas, no producción de este mes, y no baja solo.
    """
    assert cdc._gcp_behavior("Veo 3.1 Fast Video Generation") == "variable"
    assert cdc._gcp_behavior("Cloud Storage Standard Storage US") == "stock"
    assert cdc._gcp_behavior("Network Internet Egress") == "stock"


def test_railway_desglosa_por_NOMBRE_de_servicio_no_por_uuid(monkeypatch):
    """El desglose por servicio se guardaba con el UUID, no con el nombre.

    La query de `usage` sólo trae `tags { serviceId }`. Guardarlo crudo dejó
    la tabla de julio con `bdf24933-a1ab-4316-a33c-3ff161bd3b1a: $144,44`,
    que no responde la pregunta para la que existe el desglose: cuánto del
    "fijo" es `api` —residente— y cuánto son los workers, que escalan con
    los renders.

    **El mock usa UUIDs reales a propósito.** La versión anterior de este
    test mockeaba `"serviceId": "api"` —un valor que la API nunca devuelve—
    y por eso pasaba en verde con el bug puesto: afirmaba que aparecían
    nombres mientras el código guardaba UUIDs. Un mock que no puede
    distinguir el bug del arreglo no es un test.
    """
    monkeypatch.setenv("RAILWAY_API_TOKEN", "t")
    monkeypatch.setenv("RAILWAY_PROJECT_ID", "p")
    cdc._RAILWAY_SERVICE_NAMES.clear()

    API_UUID = "78364446-79a5-4d75-a6e0-9ba9b6bb0caa"
    WORKER_UUID = "bdf24933-a1ab-4316-a33c-3ff161bd3b1a"

    class _Resp:
        def __init__(self, payload): self._p = payload
        def raise_for_status(self): pass
        def json(self): return self._p

    def _post(url, **kw):
        q = (kw.get("json") or {}).get("query", "")
        if "services" in q:
            return _Resp({"data": {"project": {"services": {"edges": [
                {"node": {"id": API_UUID, "name": "api"}},
                {"node": {"id": WORKER_UUID, "name": "Worker"}}]}}}})
        return _Resp({"data": {"usage": [
            {"measurement": "MEMORY_USAGE_GB", "value": 4 * 1440,
             "tags": {"serviceId": API_UUID}},
            {"measurement": "MEMORY_USAGE_GB", "value": 6 * 1440,
             "tags": {"serviceId": WORKER_UUID}}]}})

    import requests
    monkeypatch.setattr(requests, "post", _post)

    res = cdc._railway_day(date(2026, 8, 5))
    servicios = {r["dim_value"]: r["amount_usd"]
                 for r in res.rows if r["dim_type"] == "service"}
    assert set(servicios) == {"api", "Worker"}, servicios
    # Worker consume 1,5x lo de api y eso tiene que verse.
    assert servicios["Worker"] > servicios["api"]
    # Y las mediciones siguen estando, en su propia dimensión.
    mediciones = {r["dim_value"] for r in res.rows if r["dim_type"] == "sku"}
    assert mediciones == {"MEMORY_USAGE_GB"}


def test_railway_cae_al_uuid_si_no_puede_resolver_nombres(monkeypatch):
    # Un nombre faltante no puede tirar abajo la recolección del gasto: sin
    # la fila, ese servicio desaparece del total.
    monkeypatch.setenv("RAILWAY_API_TOKEN", "t")
    monkeypatch.setenv("RAILWAY_PROJECT_ID", "p")
    cdc._RAILWAY_SERVICE_NAMES.clear()
    UUID = "bdf24933-a1ab-4316-a33c-3ff161bd3b1a"

    class _Resp:
        def raise_for_status(self): pass
        def json(self):
            return {"data": {"usage": [
                {"measurement": "MEMORY_USAGE_GB", "value": 4 * 1440,
                 "tags": {"serviceId": UUID}}]}}

    def _post(url, **kw):
        if "services" in (kw.get("json") or {}).get("query", ""):
            raise RuntimeError("la API de nombres no contesta")
        return _Resp()

    import requests
    monkeypatch.setattr(requests, "post", _post)

    res = cdc._railway_day(date(2026, 8, 5))
    servicios = {r["dim_value"] for r in res.rows if r["dim_type"] == "service"}
    assert servicios == {UUID}
    assert res.status == "ok"


# ---------------------------------------------------------------------------
# Un `ok` con $0,00 no es un día terminado
# ---------------------------------------------------------------------------

def test_un_dia_ok_pero_en_cero_se_vuelve_a_pedir(db):
    """Los proveedores publican tarde y rellenan hacia atrás.

    Medido el 1-sep-2026: la corrida de las 01:26 UTC guardó agosto con
    $0,00 del día 2 en adelante porque el export de facturación de GCP
    todavía no los tenía; catorce horas después el MISMO colector devolvía
    $124 para esos mismos días. Julio pasó de $74,20 a $138,90 entre dos
    corridas.

    Como el backfill es guiado por huecos y esos días quedaron en `ok`,
    nunca se volvían a pedir. Agosto se congelaba mal para siempre — y el
    panel lo mostraba como un mes barato.
    """
    from database import CostDaily, CostCollectionRun

    hoy = date(2026, 3, 20)
    ayer = date(2026, 3, 19)
    for d, monto in ((date(2026, 3, 18), 0.0), (ayer, 7.5)):
        db.add(CostCollectionRun(day=d, source="gcp", status="ok"))
        db.add(CostDaily(day=d, source="gcp", grain="day", dim_type="total",
                         dim_value="total", amount_usd=monto))
    db.commit()
    try:
        huecos = cdc.pending_gaps(db, today=hoy, days=4)
        assert (date(2026, 3, 18), "gcp") in huecos, "el día en cero no se re-pide"
        # El día con gasto SÍ está terminado: volver a pedirlo es gastar al pedo.
        assert (ayer, "gcp") not in huecos
    finally:
        for modelo in (CostDaily, CostCollectionRun):
            db.query(modelo).filter(modelo.source == "gcp",
                                    modelo.day >= date(2026, 3, 17),
                                    modelo.day <= hoy).delete(synchronize_session=False)
        db.commit()


def test_un_cero_viejo_ya_no_se_re_pide(db):
    """Pasada la ventana de reformulación, un cero es un cero de verdad.

    Sin este corte el colector volvería a pedir los mismos días a las APIs
    para siempre, y varias cobran por consulta.
    """
    from database import CostDaily, CostCollectionRun

    # Fechas FIJAS, no derivadas de `DIAS_RECHECK_CERO`: la primera versión
    # de este test calculaba el día viejo a partir de la constante, así que
    # al inyectarle un valor absurdo la fecha se movía con él y el test
    # seguía en verde. Un test que no puede distinguir el bug del arreglo
    # no es un test.
    hoy = date(2026, 6, 1)
    viejo = date(2026, 1, 15)          # 137 días atrás, muy fuera de los 45
    db.add(CostCollectionRun(day=viejo, source="gcp", status="ok"))
    db.add(CostDaily(day=viejo, source="gcp", grain="day", dim_type="total",
                     dim_value="total", amount_usd=0.0))
    db.commit()
    try:
        huecos = cdc.pending_gaps(db, today=hoy, days=150)
        assert (viejo, "gcp") not in huecos, (
            "un cero de hace 4 meses no se re-pide: varias APIs cobran por consulta")
        assert cdc.DIAS_RECHECK_CERO < 137, (
            "la ventana de re-chequeo tiene que dejar afuera este día")
    finally:
        for modelo in (CostDaily, CostCollectionRun):
            db.query(modelo).filter(modelo.source == "gcp",
                                    modelo.day == viejo).delete(synchronize_session=False)
        db.commit()
