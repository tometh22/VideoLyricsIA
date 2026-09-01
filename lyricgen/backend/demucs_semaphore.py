"""Semáforo global de separación de voz (demucs) — evita auto-encolarnos.

MEDIDO, NO SUPUESTO
-------------------
Cruce de 1500 predicciones de la API de Replicate contra la concurrencia
propia, EXCLUYENDO la ventana degradada del 26-29/08 para que no confunda:

    demucs simultáneos │  n  │ cola p50
    ───────────────────┼─────┼──────────
            0          │ 392 │    3,1 s
            1          │  26 │   51,6 s
            2+         │  27 │  115,1 s

`cjwbw/demucs` es un modelo público en pool compartido y se comporta como
**un solo slot serializado**: con 2 predicciones nuestras a la vez la cola se
multiplica por ~37. A 40 demucs/hora (32 canciones/h) la utilización de ese
slot llega al 97% y la cola deja de converger — crece sin techo mientras dure
el lote.

QUÉ HACE
--------
Limita cuántas separaciones nuestras están en vuelo a la vez, en TODA la flota
(el lease vive en Redis, no en el proceso). El worker **espera su turno** en
vez de fallar: demucs son ~87 s de una canción de ~350 s, así que 3 workers
comparten 1-2 slots sin quedarse quietos.

Es lo contrario de un rate limit defensivo: acá el throughput MEJORA al
limitar, porque la cola del proveedor deja de crecer.

FAIL-OPEN
---------
Sin Redis o ante cualquier error, no bloquea. Un semáforo caído no puede
detener la producción; el peor caso es volver al comportamiento actual.
"""
from __future__ import annotations

import logging
import os
import time as _t
import uuid as _uuid

logger = logging.getLogger("genly.demucs_semaphore")

_KEY = "demucs:in_flight:v2"
# 1-2 slots. Con 1: cola p50 ~3 s. Con 2: ~52 s pero +throughput. Por encima
# de 2 la cola del proveedor se dispara y el throughput CAE.
_MAX = int(os.environ.get("DEMUCS_MAX_CONCURRENT", "2"))
_BATCH_MAX = int(os.environ.get("DEMUCS_BATCH_MAX_CONCURRENT", "1"))
# TTL del lease: si un worker muere sin soltar, el slot se libera solo.
# Tiene que superar el presupuesto de demucs (1200 s) + margen.
_TTL_S = int(os.environ.get("DEMUCS_LEASE_TTL_S", "1500"))
# Cuánto espera un worker por un slot antes de seguir igual. Preferimos una
# corrida sin semáforo antes que un job muerto.
_WAIT_MAX_S = float(os.environ.get("DEMUCS_SLOT_WAIT_MAX_S", "900"))
_POLL_S = float(os.environ.get("DEMUCS_SLOT_POLL_S", "3"))

# Cada lease vive como un miembro de un sorted set cuyo score es su instante
# de expiracion. Un SET comun con EXPIRE sobre la coleccion completa parece
# suficiente, pero no lo es: cada acquire renueva el TTL de TODO el set y, con
# trafico continuo, un lease de un worker muerto puede quedar para siempre.
# El script limpia expirados y toma el slot en una unica operacion atomica.
_ACQUIRE_LUA = """
local now_parts = redis.call('TIME')
local now = tonumber(now_parts[1]) + tonumber(now_parts[2]) / 1000000
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
local count = redis.call('ZCARD', KEYS[1])
local batch_count = 0
local members = redis.call('ZRANGE', KEYS[1], 0, -1)
for _, member in ipairs(members) do
  if string.sub(member, 1, 6) == 'batch:' then
    batch_count = batch_count + 1
  end
end
local batch_allowed = ARGV[4] ~= '1' or batch_count < tonumber(ARGV[5])
if count < tonumber(ARGV[3]) and batch_allowed then
  redis.call('ZADD', KEYS[1], now + tonumber(ARGV[2]), ARGV[1])
  redis.call('EXPIRE', KEYS[1], math.ceil(tonumber(ARGV[2])) + 60)
  return count + 1
end
return -count
"""

_COUNT_LUA = """
local now_parts = redis.call('TIME')
local now = tonumber(now_parts[1]) + tonumber(now_parts[2]) / 1000000
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
return redis.call('ZCARD', KEYS[1])
"""


def _client():
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        return None
    try:
        from redis import Redis
        return Redis.from_url(url, socket_timeout=3)
    except Exception:
        return None


def acquire(*, wait_max_s: float | None = None) -> str | None:
    """Espera un slot y devuelve el lease. None = sin enforcement (seguí igual).

    Bloquea hasta `wait_max_s`. Al agotarse devuelve None y el caller sigue:
    una separación con cola larga es mejor que un job perdido.
    """
    if _MAX <= 0:
        return None
    client = _client()
    if client is None:
        return None
    workload = os.environ.get("WORKLOAD_CLASS", "interactive").strip().lower()
    is_batch = workload == "batch"
    lease = ("batch:" if is_batch else "interactive:") + _uuid.uuid4().hex[:12]
    limite = _t.monotonic() + (wait_max_s if wait_max_s is not None else _WAIT_MAX_S)
    esperado = 0.0
    while True:
        try:
            n = int(client.eval(
                _ACQUIRE_LUA, 1, _KEY, lease, _TTL_S, _MAX,
                1 if is_batch else 0, _BATCH_MAX,
            ))
        except Exception as exc:
            logger.debug("[DEMUCS-SEM] Redis no disponible (%s) — sin límite",
                         type(exc).__name__)
            return None
        if n > 0:
            if esperado > 1:
                logger.info("[DEMUCS-SEM] slot tomado tras %.0fs de espera "
                            "(%d/%d en vuelo)", esperado, n, _MAX)
            return lease
        active = -n
        if _t.monotonic() >= limite:
            logger.warning(
                "[DEMUCS-SEM] %d en vuelo (cap %d) tras %.0fs de espera — sigo "
                "SIN slot. La cola de Replicate puede alargarse.", active, _MAX, esperado)
            return None
        _t.sleep(_POLL_S)
        esperado += _POLL_S


def release(lease: str | None) -> None:
    """Devuelve el slot. Best-effort: el TTL lo libera igual si esto falla."""
    if not lease:
        return
    client = _client()
    if client is None:
        return
    try:
        client.zrem(_KEY, lease)
    except Exception:
        pass


def in_flight() -> int:
    """Cuántas separaciones hay en vuelo. Para el dashboard del lote."""
    client = _client()
    if client is None:
        return -1
    try:
        return int(client.eval(_COUNT_LUA, 1, _KEY))
    except Exception:
        return -1
