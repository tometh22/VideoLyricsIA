"""Las dos palancas de costo que salieron de los pedidos reales del cliente.

1. **Movimiento por defecto.** El 86,6% de los jobs de staging llega sin
   `movement_style`; esos caían al energy-derived, que en los estribillos
   (energía ≥0,75) devuelve "dinamico". El cliente pidió lo contrario cinco
   veces por escrito. Ahora Auto usa `BG_DEFAULT_MOVEMENT` (estático).

2. **Tope de generaciones por job.** En jul-2026, 12 jobs de prod (el 10%)
   consumieron el 42,8% del gasto de Veo, y uno llegó a 26 llamadas — ~$16
   en un video que se vende a $8.
"""

import pytest

import scenes
from scenes import Section, build_scene_plan


def _prompt_fn(background_hint="", movement_style="", section_type="",
               energy=0.0):
    return {"prompt": f"p:{movement_style}", "movement_style": movement_style}


def _sections():
    """Un verso tranquilo y un estribillo con energía — antes el estribillo
    salía con cámara en movimiento."""
    return [
        Section(type="verso", start=0.0, end=20.0, energy=0.20,
                recurrence_key="verso1"),
        Section(type="coro", start=20.0, end=40.0, energy=0.90,
                recurrence_key="coro1"),
    ]


# ---------------------------------------------------------------------------
# Movimiento por defecto
# ---------------------------------------------------------------------------

def test_energy_derived_seguia_moviendo_los_estribillos():
    """El comportamiento viejo, para dejar constancia de qué se corrige."""
    assert scenes.energy_to_movement(0.90) == "dinamico"
    assert scenes.energy_to_movement(0.20) == "estatico"


def test_auto_ahora_usa_estatico_en_todas_las_escenas(monkeypatch):
    monkeypatch.setattr(scenes, "DEFAULT_MOVEMENT_WHEN_AUTO", "estatico")
    plan = build_scene_plan(_sections(), {}, _prompt_fn, operator_movement="")
    movs = [s["movement_style"] for s in plan["scenes"]]
    assert movs == ["estatico", "estatico"], movs


def test_la_eleccion_explicita_del_operador_sigue_mandando(monkeypatch):
    """El default sólo toca el camino Auto. Si el operador eligió, manda él."""
    monkeypatch.setattr(scenes, "DEFAULT_MOVEMENT_WHEN_AUTO", "estatico")
    plan = build_scene_plan(_sections(), {}, _prompt_fn,
                            operator_movement="animado")
    assert {s["movement_style"] for s in plan["scenes"]} == {"animado"}


def test_estandar_sigue_cayendo_al_energy_derived(monkeypatch):
    """"estandar" es una elección explícita que PIDE variación por sección —
    no debe quedar aplastada por el default."""
    monkeypatch.setattr(scenes, "DEFAULT_MOVEMENT_WHEN_AUTO", "estatico")
    plan = build_scene_plan(_sections(), {}, _prompt_fn,
                            operator_movement="estandar")
    movs = [s["movement_style"] for s in plan["scenes"]]
    assert movs == ["estatico", "dinamico"], movs


def test_se_puede_volver_al_comportamiento_anterior(monkeypatch):
    """`BG_DEFAULT_MOVEMENT=""` restaura el energy-derived, por si el default
    resulta demasiado quieto para otro cliente."""
    monkeypatch.setattr(scenes, "DEFAULT_MOVEMENT_WHEN_AUTO", "")
    plan = build_scene_plan(_sections(), {}, _prompt_fn, operator_movement="")
    movs = [s["movement_style"] for s in plan["scenes"]]
    assert movs == ["estatico", "dinamico"], movs


# ---------------------------------------------------------------------------
# Tope de generaciones de Veo
# ---------------------------------------------------------------------------

def _fake_counter(monkeypatch, n):
    """Simula `n` llamadas pagas ya registradas para el job."""
    import pipeline

    class _Q:
        def filter(self, *a, **k): return self
        def scalar(self): return n

    class _S:
        def query(self, *a, **k): return _Q()
        def close(self): pass

    monkeypatch.setattr("database.SessionLocal", _S)
    return pipeline


def test_bajo_el_tope_deja_pasar(monkeypatch):
    p = _fake_counter(monkeypatch, 3)
    monkeypatch.setattr(p, "VEO_MAX_CALLS_PER_JOB", 10)
    over, spent = p._veo_budget_exceeded("job123")
    assert over is False and spent == 3


def test_en_el_tope_corta(monkeypatch):
    p = _fake_counter(monkeypatch, 10)
    monkeypatch.setattr(p, "VEO_MAX_CALLS_PER_JOB", 10)
    over, spent = p._veo_budget_exceeded("job123")
    assert over is True and spent == 10


def test_el_job_de_26_llamadas_se_habria_cortado(monkeypatch):
    """El outlier real de jul-2026: 26 generaciones en una sola canción."""
    p = _fake_counter(monkeypatch, 26)
    monkeypatch.setattr(p, "VEO_MAX_CALLS_PER_JOB", 10)
    assert p._veo_budget_exceeded("job123")[0] is True


def test_tope_en_cero_lo_desactiva(monkeypatch):
    p = _fake_counter(monkeypatch, 999)
    monkeypatch.setattr(p, "VEO_MAX_CALLS_PER_JOB", 0)
    assert p._veo_budget_exceeded("job123") == (False, 0)


def test_sin_job_id_no_topea(monkeypatch):
    p = _fake_counter(monkeypatch, 999)
    monkeypatch.setattr(p, "VEO_MAX_CALLS_PER_JOB", 10)
    assert p._veo_budget_exceeded(None) == (False, 0)
    assert p._veo_budget_exceeded("") == (False, 0)


def test_si_la_db_falla_deja_generar(monkeypatch):
    """Un tope de costo que rompe entregas cuando la DB hipa es peor que el
    gasto que evita."""
    import pipeline

    class _Boom:
        def __init__(self): raise RuntimeError("db caida")

    monkeypatch.setattr("database.SessionLocal", _Boom)
    monkeypatch.setattr(pipeline, "VEO_MAX_CALLS_PER_JOB", 10)
    assert pipeline._veo_budget_exceeded("job123") == (False, 0)


def test_la_excepcion_es_atrapable_por_los_llamadores():
    """Los tres call sites envuelven la generación en `except Exception`, así
    que el fallback a gradiente sigue andando."""
    import pipeline
    assert issubclass(pipeline.VeoBudgetExceeded, Exception)
    with pytest.raises(Exception):
        raise pipeline.VeoBudgetExceeded("test")
