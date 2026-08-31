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


def test_not_configured_counts_as_covered(db):
    """Una fuente sin credenciales es una respuesta, no un hueco.

    Si contara como hueco, `complete` nunca sería verde y el flag dejaría de
    ser señal en dos semanas.
    """
    d = date(2020, 3, 10)
    for src in cs.SOURCES:
        _run(db, d, src, status="not_configured")
    db.commit()
    assert coverage(db, d, d)["complete"] is True


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
