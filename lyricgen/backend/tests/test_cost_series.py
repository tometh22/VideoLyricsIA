"""Tests for the read side of the daily cost panel.

The theme: a panel that silently under-reports is worse than no panel. The
user's requirement was literally "100% confidence so I don't have to redo
this by hand every month", so every test here is about making an incomplete
or mis-scoped answer impossible to mistake for a complete one.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

import cost_series as cs
from cost_series import coverage, series
from database import CostCollectionRun, CostDaily


def _fact(db, day, source, dim_type, dim_value, amount, *,
          grain="day", behavior=None, qty=None, estimate=False):
    db.add(CostDaily(day=day, source=source, grain=grain, dim_type=dim_type,
                     dim_value=dim_value, amount_usd=amount, qty=qty,
                     cost_behavior=behavior, is_estimate=estimate,
                     basis="measured", fetched_at=datetime.now(timezone.utc)))


def _run(db, day, source, status="ok"):
    db.add(CostCollectionRun(day=day, source=source, status=status,
                             attempts=1, last_attempt_at=datetime.now(timezone.utc)))


# ---------------------------------------------------------------------------
# Cobertura
# ---------------------------------------------------------------------------

def test_a_day_nobody_ever_collected_is_reported_missing(db):
    """El caso que hace que el panel mienta: no hay filas ⇒ parece barato."""
    d = date(2020, 1, 10)
    for src in cs.SOURCES:
        _run(db, d, src)
    # el día siguiente no se intentó nunca
    db.commit()

    cov = coverage(db, d, d + timedelta(days=1))
    assert cov["complete"] is False
    faltantes = {(m["day"], m["source"]) for m in cov["missing"]}
    assert ((d + timedelta(days=1)).isoformat(), "gcp") in faltantes
    assert all(m["status"] == "nunca_intentado" for m in cov["missing"])


def test_an_errored_day_is_missing_not_collected(db):
    """`error` no cuenta como recolectado, aunque haya dejado rastro."""
    d = date(2020, 2, 10)
    for src in cs.SOURCES:
        _run(db, d, src, status="error" if src == "gcp" else "ok")
    db.commit()

    cov = coverage(db, d, d)
    assert cov["complete"] is False
    assert cov["missing"][0]["source"] == "gcp"
    assert cov["missing"][0]["status"] == "error"


def test_not_configured_is_a_hole_not_a_covered_cell(db):
    """Una fuente sin credenciales NO puede contar como recolectada.

    Esta aserción estuvo invertida. El argumento para contarla como cubierta
    era que si no, `complete` nunca llega a verde y el flag deja de ser
    señal. Pero la consecuencia es peor: el mes en que falte la credencial
    de GCP —el 52% de la factura— reportaría `complete: true` con GCP en $0.
    Un número más chico con cara de buena noticia, que es exactamente el
    modo de falla que este panel existe para hacer imposible.

    Que `complete` esté en rojo mientras falta una credencial es la
    respuesta correcta: el total ES un piso. Para excluir una fuente a
    propósito hay que sacarla de SOURCES, no disfrazarla de recolectada.

    Nota: `pending_gaps` SÍ la trata como resuelta, y no se contradice con
    esto — no reintentar una credencial que falta es ahorro de llamadas;
    decir que el mes está completo es mentir.
    """
    d = date(2020, 3, 10)
    for src in cs.SOURCES:
        _run(db, d, src, status="not_configured")
    db.commit()

    cov = coverage(db, d, d)
    assert cov["complete"] is False
    assert cov["collected_cells"] == 0
    assert all(m["status"] == "not_configured" for m in cov["missing"])


def test_today_is_excluded_from_coverage_because_it_is_still_accruing(db):
    hoy = datetime.now(timezone.utc).date()
    cov = coverage(db, hoy, hoy)
    assert cov["expected_cells"] == 0
    assert cov["complete"] is True


# ---------------------------------------------------------------------------
# El total no se multiplica por sumar dimensiones
# ---------------------------------------------------------------------------

def test_dimension_rows_are_not_added_on_top_of_the_total(db):
    """`total` y `sku` son el MISMO dinero visto de dos formas.

    Sumar los dos da 2x. Es el error que más fácil se comete leyendo esta
    tabla, así que hay que fijarlo.
    """
    d = date(2020, 4, 10)
    _fact(db, d, "gcp", "total", "total", 10.0, behavior="variable")
    _fact(db, d, "gcp", "sku", "Veo", 7.0, behavior="variable")
    _fact(db, d, "gcp", "sku", "Gemini", 3.0, behavior="variable")
    for src in cs.SOURCES:
        _run(db, d, src)
    db.commit()

    out = series(db, d, d, group_by="source")
    assert out["total_usd"] == pytest.approx(10.0)


def test_monthly_grain_rows_are_not_double_counted_across_a_day_range(db):
    """Una suscripción mensual vive en grain='month' y su fila `total`
    no se puede sumar una vez por cada día del rango."""
    d = date(2020, 5, 1)
    _fact(db, d, "fixed", "total", "total", 24.0, grain="month", behavior="fijo")
    for src in cs.SOURCES:
        for i in range(3):
            _run(db, d + timedelta(days=i), src)
    db.commit()

    out = series(db, d, d + timedelta(days=2), group_by="source")
    assert out["by_group"].get("fixed") == pytest.approx(24.0)


# ---------------------------------------------------------------------------
# El filtro de OpenAI se aplica AL LEER
# ---------------------------------------------------------------------------

def test_openai_filter_is_applied_at_read_time(db, monkeypatch):
    """El colector guarda la org entera; el panel decide qué es nuestro.

    En julio la org gastó $492 y Genly $19,41. Si el filtro se aplicara al
    colectar, cambiarlo después exigiría re-pedir meses que la API ya no
    expone.
    """
    d = date(2020, 6, 10)
    _fact(db, d, "openai", "line_item", "whisper", 1.0, behavior="variable")
    _fact(db, d, "openai", "line_item", "gpt-5.4, output", 99.0, behavior="variable")
    for src in cs.SOURCES:
        _run(db, d, src)
    db.commit()

    monkeypatch.setenv("OPENAI_COST_LINE_ITEMS", "whisper")
    assert series(db, d, d)["by_group"].get("openai") == pytest.approx(1.0)

    # cambiar el filtro re-interpreta la historia ya guardada, sin re-colectar
    monkeypatch.setenv("OPENAI_COST_LINE_ITEMS", "whisper,gpt-5.4")
    assert series(db, d, d)["by_group"].get("openai") == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# Fijo vs variable
# ---------------------------------------------------------------------------

def test_group_by_behavior_separates_the_fixed_floor_from_the_marginal(db):
    """Sin este corte, el "$/video" baja al subir el volumen y hace parecer
    una mejora lo que en realidad recorta la ganancia absoluta."""
    d = date(2020, 7, 10)
    _fact(db, d, "railway", "total", "total", 100.0, behavior="fijo")
    _fact(db, d, "gcp", "total", "total", 30.0, behavior="variable")
    _fact(db, d, "r2", "total", "total", 20.0, behavior="stock")
    for src in cs.SOURCES:
        _run(db, d, src)
    db.commit()

    out = series(db, d, d, group_by="behavior")
    assert out["by_group"] == {"fijo": 100.0, "variable": 30.0, "stock": 20.0}


def test_estimated_share_exposes_how_much_is_our_model_not_an_invoice(db):
    """~43% del gasto no es una factura sino una métrica valorizada por
    nosotros. `complete: true` no distingue eso; este campo sí."""
    d = date(2020, 8, 10)
    _fact(db, d, "gcp", "total", "total", 60.0, behavior="variable", estimate=False)
    _fact(db, d, "railway", "total", "total", 40.0, behavior="fijo", estimate=True)
    for src in cs.SOURCES:
        _run(db, d, src)
    db.commit()

    out = series(db, d, d)
    assert out["invoiced_usd"] == pytest.approx(60.0)
    assert out["estimated_usd"] == pytest.approx(40.0)
    assert out["estimated_share"] == pytest.approx(0.4)


# ---------------------------------------------------------------------------
# Granularidad
# ---------------------------------------------------------------------------

def test_week_buckets_start_on_monday(db):
    """Si no, "semana" significa algo distinto según el día en que mirés."""
    martes = date(2020, 9, 8)      # 2020-09-08 es martes
    jueves = date(2020, 9, 10)
    for d in (martes, jueves):
        _fact(db, d, "gcp", "total", "total", 5.0, behavior="variable")
        for src in cs.SOURCES:
            _run(db, d, src)
    db.commit()

    out = series(db, martes, jueves, granularity="week")
    assert len(out["series"]) == 1
    assert out["series"][0]["bucket"] == "2020-09-07"   # lunes
    assert out["series"][0]["total"] == pytest.approx(10.0)


def test_invalid_granularity_is_rejected(db):
    with pytest.raises(ValueError):
        series(db, date(2020, 10, 1), date(2020, 10, 2), granularity="hora")


def test_openai_is_not_counted_twice_when_grouping_by_behavior(db, monkeypatch):
    """OpenAI tiene fila `total` (la org entera) Y filas `line_item`.

    El agregado las trata distinto: descarta el `total` y reconstruye desde
    los `line_item` filtrados. Si alguna rama del código olvidara ese
    descarte, OpenAI entraría dos veces — una con el total de la org sin
    filtrar. Con $492 de org contra $19 nuestros, el error sería de 25x.
    """
    d = date(2021, 1, 10)
    _fact(db, d, "openai", "total", "total", 492.0, behavior="variable")
    _fact(db, d, "openai", "line_item", "whisper", 19.0, behavior="variable")
    _fact(db, d, "openai", "line_item", "gpt-5.4, output", 473.0, behavior="variable")
    for src in cs.SOURCES:
        _run(db, d, src)
    db.commit()

    monkeypatch.setenv("OPENAI_COST_LINE_ITEMS", "whisper")
    for group_by in ("source", "behavior"):
        out = series(db, d, d, group_by=group_by)
        assert out["total_usd"] == pytest.approx(19.0), (
            f"group_by={group_by} dio ${out['total_usd']}, esperaba $19 "
            f"(si dio 511 se contó el total de la org además de los line_items)"
        )


def test_sku_view_includes_openai_and_totals_match_the_source_view(db, monkeypatch):
    """Las vistas por SKU y por fuente tienen que dar el mismo total.

    Las filas de OpenAI son `line_item`, no `sku`. Pedir sólo `sku` hacía
    que OpenAI desapareciera del desglose fino sin aviso: dos vistas del
    mismo rango con totales distintos y nada que lo explicara.
    """
    d = date(2021, 2, 10)
    _fact(db, d, "gcp", "total", "total", 10.0, behavior="variable")
    _fact(db, d, "gcp", "sku", "Veo", 10.0, behavior="variable")
    _fact(db, d, "openai", "total", "total", 100.0, behavior="variable")
    _fact(db, d, "openai", "line_item", "whisper", 4.0, behavior="variable")
    _fact(db, d, "openai", "line_item", "gpt-5.4, output", 96.0, behavior="variable")
    for src in cs.SOURCES:
        _run(db, d, src)
    db.commit()

    monkeypatch.setenv("OPENAI_COST_LINE_ITEMS", "whisper")
    por_fuente = series(db, d, d, group_by="source")
    por_sku = series(db, d, d, group_by="sku")

    assert por_fuente["total_usd"] == pytest.approx(14.0)
    assert por_sku["total_usd"] == pytest.approx(14.0), "las dos vistas no cuadran"
    assert "openai:whisper" in por_sku["by_group"]
    assert "openai:gpt-5.4, output" not in por_sku["by_group"]


# ---------------------------------------------------------------------------
# Franquicias mensuales — la función existía y NUNCA se llamaba
# ---------------------------------------------------------------------------

def test_r2_free_tier_is_subtracted_on_a_full_month(db, monkeypatch):
    """El colector guarda crudo justamente para poder restar esto acá.

    `monthly_adjustments` estuvo escrita y sin ningún llamador: la
    franquicia de R2 nunca se restaba y el "principio rector" (reglas del
    mes al leer) era una promesa vacía.
    """
    monkeypatch.setenv("R2_APPLY_FREE_TIER", "1")
    d0, d1 = date(2021, 4, 1), date(2021, 4, 30)
    d = d0
    while d <= d1:
        # 100 GB constantes: la franquicia son 10 GB-mes.
        _fact(db, d, "r2", "sku", "storage", 0.05, qty=100.0, behavior="stock")
        _fact(db, d, "r2", "total", "total", 0.05, behavior="stock")
        for src in cs.SOURCES:
            _run(db, d, src)
        d += timedelta(days=1)
    db.commit()

    aj = cs.monthly_adjustments(db, d0, d1)
    assert aj["aplicables"] is True
    storage = next(a for a in aj["ajustes"] if a["concepto"] == "r2_franquicia_storage")
    # 10 GB-mes gratis x $0,015 = -$0,15
    assert storage["amount_usd"] == pytest.approx(-0.15)

    out = series(db, d0, d1)
    assert out["monthly_adjustments"]["total_usd"] == pytest.approx(-0.15)


def test_free_tier_is_not_prorated_over_a_partial_range(db, monkeypatch):
    """Sobre una semana la franquicia MENSUAL no significa nada.

    Restar 1/31 por día tampoco sirve: el excedente no es lineal, así que
    prorratearla sería inventar un número.
    """
    monkeypatch.setenv("R2_APPLY_FREE_TIER", "1")
    d0, d1 = date(2021, 5, 3), date(2021, 5, 9)
    d = d0
    while d <= d1:
        _fact(db, d, "r2", "sku", "storage", 0.05, qty=100.0, behavior="stock")
        for src in cs.SOURCES:
            _run(db, d, src)
        d += timedelta(days=1)
    db.commit()

    aj = cs.monthly_adjustments(db, d0, d1)
    assert aj["aplicables"] is False
    assert aj["total_usd"] == 0.0
    assert "mes completo" in aj["motivo"]



# ---------------------------------------------------------------------------
# `stale_sources` — el ok que miente
# ---------------------------------------------------------------------------
#
# Medido en staging el 1-sep-2026: agosto tenía las 31 celdas de GCP en `ok`
# y `complete: true`, con 30 de esos 31 días en $0,00 exacto porque el export
# de facturación a BigQuery cortaba el 1-ago. El panel mostraba GCP en $3,97
# contra $138,90 de julio, en verde. Leído como ahorro, es un 97% de gasto
# desaparecido justo antes de repartir utilidades.

def _sembrar(db, source, dia_a_monto, grain="day"):
    # Limpia primero: otros tests de este archivo ya sembraron celdas y la
    # PK de cost_collection_runs es (day, source).
    from database import CostDaily, CostCollectionRun
    _limpiar(db, source, min(dia_a_monto), max(dia_a_monto))
    for d, monto in dia_a_monto.items():
        db.add(CostDaily(day=d, source=source, grain=grain, dim_type="total",
                         dim_value="total", amount_usd=monto))
        db.add(CostCollectionRun(day=d, source=source, status="ok"))
    db.commit()


def test_marca_la_fuente_que_contesta_ok_pero_devuelve_cero(db):
    from datetime import date, timedelta
    import cost_series

    ini = date(2022, 3, 1)
    # Gasto hasta el día 5, cero desde el 6 al 20: el export se cortó.
    montos = {ini + timedelta(days=i): (4.0 if i <= 4 else 0.0) for i in range(20)}
    _sembrar(db, "gcp", montos)
    try:
        avisos = cost_series.stale_sources(db, ini, ini + timedelta(days=19))
        gcp = next(a for a in avisos if a["source"] == "gcp")
        assert gcp["last_nonzero_day"] == (ini + timedelta(days=4)).isoformat()
        assert gcp["zero_days"] == 15
        # El total reportado NO es el total: es un piso.
        assert gcp["reported_usd"] == 20.0
    finally:
        _limpiar(db, "gcp", ini, ini + timedelta(days=19))


def test_un_dia_tranquilo_suelto_no_dispara_el_aviso(db):
    from datetime import date, timedelta
    import cost_series

    ini = date(2022, 4, 1)
    # Dos días en cero al final: por debajo del umbral. Un proveedor puede
    # no cobrar un fin de semana sin renders.
    montos = {ini + timedelta(days=i): (4.0 if i < 8 else 0.0) for i in range(10)}
    _sembrar(db, "replicate", montos)
    try:
        avisos = cost_series.stale_sources(db, ini, ini + timedelta(days=9))
        assert not [a for a in avisos if a["source"] == "replicate"]
    finally:
        _limpiar(db, "replicate", ini, ini + timedelta(days=9))


def test_una_fuente_sin_ningun_gasto_no_se_marca(db):
    # Sin un solo día con gasto no hay contra qué comparar: puede ser una
    # fuente que de verdad no se usó. Ese caso lo cubre `coverage`.
    from datetime import date, timedelta
    import cost_series

    ini = date(2022, 5, 1)
    _sembrar(db, "replicate", {ini + timedelta(days=i): 0.0 for i in range(10)})
    try:
        assert not cost_series.stale_sources(db, ini, ini + timedelta(days=9))
    finally:
        _limpiar(db, "replicate", ini, ini + timedelta(days=9))


def test_las_suscripciones_mensuales_no_se_marcan(db):
    # Grano mensual: una fila por mes y el resto de los días en cero por
    # diseño. Marcarlas sería ruido permanente.
    from datetime import date, timedelta
    import cost_series

    ini = date(2022, 6, 1)
    _sembrar(db, "fixed", {ini: 24.0}, grain="month")
    try:
        assert not cost_series.stale_sources(db, ini, ini + timedelta(days=19))
    finally:
        _limpiar(db, "fixed", ini, ini + timedelta(days=19))


def _limpiar(db, source, desde, hasta):
    from database import CostDaily, CostCollectionRun
    for modelo in (CostDaily, CostCollectionRun):
        db.query(modelo).filter(modelo.source == source, modelo.day >= desde,
                                modelo.day <= hasta).delete(synchronize_session=False)
    db.commit()
