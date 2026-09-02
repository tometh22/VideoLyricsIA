"""Tripwire de saturación del pool de Postgres — indicador LÍDER.

Por qué existe (decisión 2026-07, "qué haría un CTO senior"): el pool está
topeado a pool_size+max_overflow=10 por proceso por el max_connections=100 del
plan de Railway (ver database.py). No se puede crecer en código sin PgBouncer o
un plan más grande. En vez de reaccionar cuando el `QueuePool limit ... timeout`
ya le pega a un cliente (indicador RETRASADO), medimos la saturación y avisamos
ANTES: si el uso del pool se sostiene por encima del umbral, emitimos UN evento
Sentry `[DB-POOL-ALERT]` (fingerprint estable → un solo issue, alertable) que el
Sentinel levanta y te manda a Telegram. Convierte "ojalá me acuerde de mirar el
pool" en "el sistema me avisa cuando me acerco a la pared".

Diseño:
- Muestrea `database.pool_stats()` cada SAMPLE_SECONDS.
- Saturación = checked_out / total_capacity (cuando llega a 1.0, el próximo
  checkout bloquea → timeout de 30s → error al cliente).
- Dispara sólo con saturación SOSTENIDA (SUSTAINED_SAMPLES muestras seguidas ≥
  THRESHOLD), no con un pico puntual — los bursts son normales.
- Cooldown para no spammear: tras disparar, calla COOLDOWN_MIN minutos.
- Núcleo (`saturation`, `PoolTripwire`) es puro y testeable; el thread sólo
  orquesta sleep + emisión.
"""

import logging
import os
import threading
import time

logger = logging.getLogger("genly.db_pool_watchdog")

THRESHOLD = float(os.environ.get("DB_POOL_ALERT_THRESHOLD", "0.85"))
SUSTAINED_SAMPLES = int(os.environ.get("DB_POOL_ALERT_SUSTAINED_SAMPLES", "4"))
SAMPLE_SECONDS = int(os.environ.get("DB_POOL_ALERT_SAMPLE_SECONDS", "15"))
COOLDOWN_MIN = int(os.environ.get("DB_POOL_ALERT_COOLDOWN_MIN", "30"))


def saturation(stats: dict) -> float | None:
    """Fracción del pool en uso [0..1], o None si no hay datos (SQLite/tests).

    checked_out incluye las conexiones de overflow ya abiertas; total_capacity
    es pool_size + max_overflow. En 1.0 el próximo checkout se bloquea.
    """
    cap = stats.get("total_capacity")
    if not cap:
        return None
    checked_out = stats.get("checked_out")
    if checked_out is None:
        return None
    return checked_out / cap


class PoolTripwire:
    """Máquina de estado pura: le das muestras de saturación y te dice si
    hay que ALERTAR ahora (sostenida sobre umbral, respetando el cooldown)."""

    def __init__(self, threshold: float = THRESHOLD,
                 sustained: int = SUSTAINED_SAMPLES,
                 cooldown_s: float = COOLDOWN_MIN * 60):
        self.threshold = threshold
        self.sustained = sustained
        self.cooldown_s = cooldown_s
        self._consecutive = 0
        self._last_alert_ts: float | None = None

    def observe(self, sat: float | None, now: float) -> bool:
        """Registra una muestra. Devuelve True SÓLO en la transición a alerta."""
        if sat is None or sat < self.threshold:
            self._consecutive = 0
            return False
        self._consecutive += 1
        if self._consecutive < self.sustained:
            return False
        # Sostenido sobre umbral. ¿Fuera de cooldown?
        if (self._last_alert_ts is not None
                and now - self._last_alert_ts < self.cooldown_s):
            return False
        self._last_alert_ts = now
        # Reset para exigir otra racha completa antes del próximo aviso.
        self._consecutive = 0
        return True


def _emit_alert(sat: float, stats: dict) -> None:
    pct = round(sat * 100)
    logger.warning(
        "[DB-POOL-ALERT] saturación del pool sostenida %d%% (checked_out=%s/"
        "%s, overflow=%s) — cerca del techo; ver PgBouncer/plan (docs/SCALING.md)",
        pct, stats.get("checked_out"), stats.get("total_capacity"),
        stats.get("overflow"),
    )
    try:
        import sentry_sdk
        with sentry_sdk.push_scope() as scope:
            scope.fingerprint = ["db-pool-saturation"]  # un solo issue, alertable
            scope.level = "warning"
            scope.set_tag("db_pool.saturation_pct", pct)
            scope.set_extra("pool_stats", stats)
            sentry_sdk.capture_message(
                f"[DB-POOL-ALERT] pool Postgres saturado {pct}% sostenido "
                f"(techo {stats.get('total_capacity')}/proceso)",
                level="warning",
            )
    except Exception:
        pass


def _loop() -> None:
    time.sleep(120)  # dejar que la API levante y el tráfico se estabilice
    import database
    trip = PoolTripwire()
    while True:
        try:
            stats = database.pool_stats()
            sat = saturation(stats)
            if trip.observe(sat, time.monotonic()):
                _emit_alert(sat, stats)
        except Exception:  # pragma: no cover — nunca tirar el thread
            try:
                import sentry_sdk
                sentry_sdk.capture_exception()
            except Exception:
                pass
        time.sleep(SAMPLE_SECONDS)


def start() -> None:
    """Arranca el watchdog en un daemon thread (idempotente por nombre)."""
    for t in threading.enumerate():
        if t.name == "db-pool-watchdog":
            return
    threading.Thread(target=_loop, daemon=True, name="db-pool-watchdog").start()
    logger.info("db-pool-watchdog thread started (threshold=%.0f%%, sustained=%d×%ds)",
                THRESHOLD * 100, SUSTAINED_SAMPLES, SAMPLE_SECONDS)
