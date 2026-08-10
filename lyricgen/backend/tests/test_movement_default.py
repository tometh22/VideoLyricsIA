"""Movimiento por defecto de los fondos — cambio de PRODUCTO, apagado por defecto.

Medido en ago-2026: el 86,6% de los jobs de staging llega sin
`movement_style`, y esos caen al energy-derived, que para energía >=0,75 (los
estribillos) devuelve "dinamico". UMG pidió lo contrario cinco veces por
escrito: "cambiar el fondo más estático", "un fondo sin tormenta y que no sea
animado", "más tranqui o sin loop".

El cambio altera el VIDEO que ve el cliente, así que entra APAGADO y se
habilita por tenant. Eso permite el canary: prenderlo para un cliente, mirar
el resultado, y recién después ampliar.
"""

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

