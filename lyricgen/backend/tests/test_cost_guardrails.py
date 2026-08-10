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


TENANT = "universal_argentina"


def _habilitar(monkeypatch, tenants=TENANT):
    monkeypatch.setattr(scenes, "DEFAULT_MOVEMENT_WHEN_AUTO", "estatico")
    monkeypatch.setattr(scenes, "DEFAULT_MOVEMENT_TENANTS",
                        frozenset(t.strip() for t in tenants.split(",")))


def test_apagado_por_defecto_no_cambia_nada(monkeypatch):
    """Contrato principal: sin configurar nada, el video que ve TODO cliente
    queda igual que antes (energy-derived). Un cambio que altera el
    entregable no puede entrar prendido."""
    monkeypatch.setattr(scenes, "DEFAULT_MOVEMENT_WHEN_AUTO", "")
    monkeypatch.setattr(scenes, "DEFAULT_MOVEMENT_TENANTS", frozenset())
    plan = build_scene_plan(_sections(), {}, _prompt_fn,
                            operator_movement="", tenant_id=TENANT)
    assert [s["movement_style"] for s in plan["scenes"]] == \
        ["estatico", "dinamico"]


def test_auto_usa_estatico_en_el_tenant_habilitado(monkeypatch):
    _habilitar(monkeypatch)
    plan = build_scene_plan(_sections(), {}, _prompt_fn,
                            operator_movement="", tenant_id=TENANT)
    assert [s["movement_style"] for s in plan["scenes"]] == \
        ["estatico", "estatico"]


def test_los_demas_tenants_no_se_tocan(monkeypatch):
    """El canary: se prende para un cliente y el resto sigue exactamente
    igual, así se puede mirar el resultado antes de ampliar."""
    _habilitar(monkeypatch)
    plan = build_scene_plan(_sections(), {}, _prompt_fn,
                            operator_movement="", tenant_id="otro_sello")
    assert [s["movement_style"] for s in plan["scenes"]] == \
        ["estatico", "dinamico"]


def test_la_eleccion_explicita_del_operador_sigue_mandando(monkeypatch):
    """El default sólo toca el camino Auto. Si el operador eligió, manda él."""
    _habilitar(monkeypatch)
    plan = build_scene_plan(_sections(), {}, _prompt_fn,
                            operator_movement="animado", tenant_id=TENANT)
    assert {s["movement_style"] for s in plan["scenes"]} == {"animado"}


def test_estandar_sigue_cayendo_al_energy_derived(monkeypatch):
    """"estandar" es una elección explícita que PIDE variación por sección —
    no debe quedar aplastada por el default."""
    _habilitar(monkeypatch)
    plan = build_scene_plan(_sections(), {}, _prompt_fn,
                            operator_movement="estandar", tenant_id=TENANT)
    assert [s["movement_style"] for s in plan["scenes"]] == \
        ["estatico", "dinamico"]


def test_asterisco_aplica_a_todos(monkeypatch):
    _habilitar(monkeypatch, tenants="*")
    plan = build_scene_plan(_sections(), {}, _prompt_fn,
                            operator_movement="", tenant_id="cualquiera")
    assert {s["movement_style"] for s in plan["scenes"]} == {"estatico"}


# ---------------------------------------------------------------------------
# Tope de generaciones de Veo
# ---------------------------------------------------------------------------

def _fake_counter(monkeypatch, n, artist="Bersuit", title="La Argentinidad"):
    """Sesión falsa: `n` llamadas pagas ya registradas, y un job con la
    identidad de canción indicada (o sin metadata si van vacías)."""
    import pipeline

    class _Q:
        def filter(self, *a, **k): return self
        def scalar(self): return n
        def one_or_none(self): return (artist, title)
        def all(self): return [("job-hermano-1",), ("job-hermano-2",)]

    class _S:
        def query(self, *a, **k): return _Q()
        def close(self): pass

    monkeypatch.setattr("database.SessionLocal", _S)
    return pipeline


def test_bajo_el_tope_deja_pasar(monkeypatch):
    p = _fake_counter(monkeypatch, 3)
    monkeypatch.setattr(p, "VEO_MAX_CALLS_PER_SONG", 10)
    over, spent = p._veo_budget_exceeded("job123")
    assert over is False and spent == 3


def test_en_el_tope_corta(monkeypatch):
    p = _fake_counter(monkeypatch, 10)
    monkeypatch.setattr(p, "VEO_MAX_CALLS_PER_SONG", 10)
    over, spent = p._veo_budget_exceeded("job123")
    assert over is True and spent == 10


def test_el_job_de_26_llamadas_se_habria_cortado(monkeypatch):
    """El outlier real de jul-2026: 26 generaciones en una sola canción."""
    p = _fake_counter(monkeypatch, 26)
    monkeypatch.setattr(p, "VEO_MAX_CALLS_PER_SONG", 10)
    assert p._veo_budget_exceeded("job123")[0] is True


def test_el_presupuesto_es_POR_CANCION_no_por_job(monkeypatch):
    """El punto de la revisión: un tope por job se esquiva solo, porque cada
    edición o re-render crea un job NUEVO con presupuesto fresco. Una canción
    que pasa por 5 ediciones a 10 llamadas gastaría 50 sin que ningún tope se
    entere.

    Acá el job es nuevo (0 llamadas propias) pero su canción ya acumuló 12
    entre jobs hermanos → tiene que cortar igual.
    """
    p = _fake_counter(monkeypatch, 12, artist="Bersuit", title="La Argentinidad")
    monkeypatch.setattr(p, "VEO_MAX_CALLS_PER_SONG", 10)
    over, spent = p._veo_budget_exceeded("job-recien-creado")
    assert over is True and spent == 12


def test_sin_metadata_de_cancion_cae_a_contar_solo_el_job(monkeypatch):
    """Los previews sin artista/título no tienen identidad de canción; contar
    sólo ese job es lo más ajustado posible sin inventar una."""
    p = _fake_counter(monkeypatch, 11, artist="", title="")
    monkeypatch.setattr(p, "VEO_MAX_CALLS_PER_SONG", 10)
    assert p._veo_budget_exceeded("job-sin-metadata")[0] is True


def test_tope_en_cero_lo_desactiva(monkeypatch):
    p = _fake_counter(monkeypatch, 999)
    monkeypatch.setattr(p, "VEO_MAX_CALLS_PER_SONG", 0)
    assert p._veo_budget_exceeded("job123") == (False, 0)


def test_sin_job_id_no_topea(monkeypatch):
    p = _fake_counter(monkeypatch, 999)
    monkeypatch.setattr(p, "VEO_MAX_CALLS_PER_SONG", 10)
    assert p._veo_budget_exceeded(None) == (False, 0)
    assert p._veo_budget_exceeded("") == (False, 0)


def test_si_la_db_falla_deja_generar(monkeypatch):
    """Un tope de costo que rompe entregas cuando la DB hipa es peor que el
    gasto que evita."""
    import pipeline

    class _Boom:
        def __init__(self): raise RuntimeError("db caida")

    monkeypatch.setattr("database.SessionLocal", _Boom)
    monkeypatch.setattr(pipeline, "VEO_MAX_CALLS_PER_SONG", 10)
    assert pipeline._veo_budget_exceeded("job123") == (False, 0)


def test_la_excepcion_es_atrapable_por_los_llamadores():
    """Los tres call sites envuelven la generación en `except Exception`, así
    que el fallback a gradiente sigue andando."""
    import pipeline
    assert issubclass(pipeline.VeoBudgetExceeded, Exception)
    with pytest.raises(Exception):
        raise pipeline.VeoBudgetExceeded("test")
