"""Semáforo global de demucs — limitar AUMENTA el throughput.

Medido sobre 1500 predicciones de Replicate (excluyendo la ventana degradada
del 26-29/08): `cjwbw/demucs` se comporta como un solo slot serializado.

    demucs simultáneos │  n  │ cola p50
            0          │ 392 │    3,1 s
            1          │  26 │   51,6 s
            2+         │  27 │  115,1 s

Con 2 predicciones nuestras a la vez la cola se multiplica por ~37. A 40
demucs/hora la utilización llega al 97% y la cola deja de converger.

El semáforo hace ESPERAR, no fallar: demucs son ~87 s de una canción de ~350,
así que 3 workers comparten 1-2 slots sin quedarse quietos.
"""
from __future__ import annotations

import time

import pytest

import demucs_semaphore as sem


class _FakeRedis:
    """Redis en memoria con lo justo para los scripts atomicos del modulo."""

    def __init__(self):
        self.zsets = {}
        self.now = time.time()

    def _prune(self, key):
        values = self.zsets.setdefault(key, {})
        for member, expires_at in list(values.items()):
            if expires_at <= self.now:
                del values[member]

    def eval(self, script, numkeys, key, *args):
        assert numkeys == 1
        self._prune(key)
        if script == sem._COUNT_LUA:
            return len(self.zsets[key])
        lease, ttl, cap, is_batch, batch_cap = args
        batch_count = sum(str(member).startswith("batch:") for member in self.zsets[key])
        if len(self.zsets[key]) >= int(cap) or (int(is_batch) and batch_count >= int(batch_cap)):
            return -len(self.zsets[key])
        self.zsets[key][lease] = self.now + float(ttl)
        return len(self.zsets[key])

    def zrem(self, key, val):
        return int(self.zsets.setdefault(key, {}).pop(val, None) is not None)

    def scard(self, key):
        self._prune(key)
        return len(self.zsets.setdefault(key, {}))


@pytest.fixture
def redis_fake(monkeypatch):
    r = _FakeRedis()
    monkeypatch.setenv("REDIS_URL", "redis://fake")
    monkeypatch.setattr(sem, "_client", lambda: r)
    monkeypatch.setattr(sem, "_POLL_S", 0.01)
    return r


def test_sin_redis_no_limita(monkeypatch):
    """Fail-open: un semáforo caído no puede detener la producción."""
    monkeypatch.setattr(sem, "_client", lambda: None)
    assert sem.acquire() is None


def test_da_slots_hasta_el_cap(redis_fake, monkeypatch):
    monkeypatch.setattr(sem, "_MAX", 2)
    a, b = sem.acquire(), sem.acquire()
    assert a and b and a != b
    assert redis_fake.scard(sem._KEY) == 2


def test_bloquea_al_llegar_al_cap_y_libera_al_soltar(redis_fake, monkeypatch):
    """El tercero espera; apenas se suelta uno, entra."""
    monkeypatch.setattr(sem, "_MAX", 2)
    a = sem.acquire()
    sem.acquire()
    # con espera corta el tercero NO consigue slot
    assert sem.acquire(wait_max_s=0.05) is None
    assert redis_fake.scard(sem._KEY) == 2, "el rechazado no debe dejar su marca"
    sem.release(a)
    assert sem.acquire(wait_max_s=0.05) is not None


def test_al_agotar_la_espera_sigue_sin_slot(redis_fake, monkeypatch):
    """Preferimos una separación con cola larga antes que un job perdido:
    devuelve None y el caller CONTINÚA, no falla."""
    monkeypatch.setattr(sem, "_MAX", 1)
    sem.acquire()
    assert sem.acquire(wait_max_s=0.05) is None


def test_release_es_idempotente_y_tolera_none(redis_fake, monkeypatch):
    monkeypatch.setattr(sem, "_MAX", 1)
    a = sem.acquire()
    sem.release(a)
    sem.release(a)
    sem.release(None)
    assert redis_fake.scard(sem._KEY) == 0


def test_lease_vencido_se_libera_aunque_haya_trafico(redis_fake, monkeypatch):
    """Un worker muerto no ocupa el slot para siempre.

    Regresion del primer diseno: un EXPIRE compartido se renovaba con cada
    acquire y mantenia vivos leases ajenos bajo trafico continuo.
    """
    monkeypatch.setattr(sem, "_MAX", 1)
    monkeypatch.setattr(sem, "_TTL_S", 10)
    first = sem.acquire()
    assert first
    redis_fake.now += 11
    second = sem.acquire()
    assert second and second != first
    assert sem.in_flight() == 1


def test_cap_cero_desactiva(monkeypatch, redis_fake):
    monkeypatch.setattr(sem, "_MAX", 0)
    assert sem.acquire() is None


def test_in_flight_reporta_para_el_dashboard(redis_fake, monkeypatch):
    monkeypatch.setattr(sem, "_MAX", 3)
    sem.acquire()
    sem.acquire()
    assert sem.in_flight() == 2


def test_batch_usa_como_max_un_slot_y_reserva_otro_para_interactivo(redis_fake, monkeypatch):
    monkeypatch.setattr(sem, "_MAX", 2)
    monkeypatch.setattr(sem, "_BATCH_MAX", 1)
    monkeypatch.setenv("WORKLOAD_CLASS", "batch")
    assert sem.acquire()
    assert sem.acquire(wait_max_s=0.03) is None
    monkeypatch.setenv("WORKLOAD_CLASS", "interactive")
    assert sem.acquire(wait_max_s=0.03)
    assert sem.in_flight() == 2
