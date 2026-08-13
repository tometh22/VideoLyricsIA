"""Tests del tripwire de saturación del pool (db_pool_watchdog).

Núcleo puro: saturation() + PoolTripwire (sostenido sobre umbral + cooldown).
El thread/emisión Sentry no se testean acá (I/O); se pinnea el wiring por
lectura de fuente.
"""
import inspect

import db_pool_watchdog as w


def test_saturation_from_pool_stats():
    assert w.saturation({"checked_out": 5, "total_capacity": 10}) == 0.5
    assert w.saturation({"checked_out": 10, "total_capacity": 10}) == 1.0
    # sin datos (SQLite/tests) → None, no rompe
    assert w.saturation({}) is None
    assert w.saturation({"total_capacity": 0}) is None
    assert w.saturation({"total_capacity": 10}) is None


def test_tripwire_needs_sustained_not_a_spike():
    t = w.PoolTripwire(threshold=0.85, sustained=4, cooldown_s=1000)
    now = 0.0
    # 3 muestras altas: todavía no (un burst no dispara)
    for _ in range(3):
        assert t.observe(0.9, now) is False
    # la 4ª consecutiva → alerta
    assert t.observe(0.9, now) is True


def test_tripwire_resets_on_dip_below_threshold():
    t = w.PoolTripwire(threshold=0.85, sustained=3, cooldown_s=1000)
    assert t.observe(0.9, 0) is False
    assert t.observe(0.9, 0) is False
    assert t.observe(0.5, 0) is False   # baja → resetea la racha
    assert t.observe(0.9, 0) is False   # empieza de nuevo
    assert t.observe(0.9, 0) is False
    assert t.observe(0.9, 0) is True    # ahora sí, 3 seguidas


def test_tripwire_cooldown_suppresses_repeat_alerts():
    t = w.PoolTripwire(threshold=0.85, sustained=1, cooldown_s=1800)
    assert t.observe(0.95, now := 1000.0) is True     # primer aviso
    assert t.observe(0.95, now + 60) is False         # dentro del cooldown → calla
    assert t.observe(0.95, now + 1801) is True        # pasado el cooldown → re-avisa


def test_none_saturation_never_alerts():
    t = w.PoolTripwire(threshold=0.85, sustained=1, cooldown_s=0)
    # SQLite/tests reportan {} → saturation None → jamás dispara
    assert t.observe(None, 0) is False
    assert t.observe(None, 100) is False


def test_startup_wires_the_watchdog():
    import main
    src = inspect.getsource(main)
    assert "db_pool_watchdog" in src and ".start()" in src
    # emite a Sentry con fingerprint estable (un solo issue alertable)
    emit = inspect.getsource(w._emit_alert)
    assert '["db-pool-saturation"]' in emit and "capture_message" in emit
