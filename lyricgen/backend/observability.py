"""Observability: Sentry, structured logging, health checks.

All helpers are no-ops when the relevant env vars are missing, so this module
never becomes the reason a local dev session fails to start.
"""

import contextvars
import json
import logging
import os
import shutil
import sys
import time

SENTRY_DSN = os.environ.get("SENTRY_DSN", "").strip()
# Single source of truth for environment label. ENVIRONMENT is the Railway /
# Heroku convention; ENV stays as a back-compat alias.
ENV = (os.environ.get("ENVIRONMENT")
       or os.environ.get("ENV")
       or "dev").lower().strip()
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()


def _resolve_release() -> str:
    """Release tag attached to every Sentry event so a spike can be
    correlated to the exact deploy that introduced it.

    Precedence:
      1. SENTRY_RELEASE — explicit operator override.
      2. RAILWAY_GIT_COMMIT_SHA — injected by Railway on every build,
         so API and Worker events are tagged with the commit they run.
      3. Static fallback so events are never untagged (matches the old
         hard-coded value from main.py's pre-reconcile init).
    """
    return (os.environ.get("SENTRY_RELEASE", "").strip()
            or os.environ.get("RAILWAY_GIT_COMMIT_SHA", "").strip()
            or "genly@2.0.0")


_CODE_FINGERPRINT: str | None = None


def backend_code_fingerprint() -> str:
    """Huella del CÓDIGO DE BACKEND que este proceso tiene cargado.

    Por qué existe: el gate de flota comparaba el SHA de git entre la API y los
    workers, y eso confunde dos cosas muy distintas.

    Los servicios de worker tienen filtros de path en Railway, así que un commit
    que sólo toca el frontend NO los redeploya — y hace bien, no hay nada nuevo
    que correr ahí. Pero el SHA de la API sí avanza. Resultado: después de CADA
    merge de sólo-frontend, /health reportaba `down` con la base arriba, Redis
    arriba, los 10 workers vivos y las colas vacías. Una alarma que grita en
    falso de rutina es peor que no tenerla: enseña a ignorarla, y el día que un
    worker quede atrás de verdad nadie va a mirar.

    La huella compara lo que importa: si el código Python que corre la API y el
    que corren los workers es el mismo, son compatibles, sin importar cuántos
    commits de frontend haya en el medio. Y si el backend SÍ cambió, la huella
    cambia y el gate sigue avisando — no se pierde señal real.

    Se calcula una vez y se cachea. Sólo los .py del directorio backend, con la
    ruta relativa dentro del hash para que renombrar un archivo cuente como
    cambio. Ante cualquier error devuelve "" y el gate cae a comparar el SHA
    (el comportamiento viejo), que es el fallback seguro.
    """
    global _CODE_FINGERPRINT
    if _CODE_FINGERPRINT is not None:
        return _CODE_FINGERPRINT
    try:
        import hashlib

        root = os.path.dirname(os.path.abspath(__file__))
        digest = hashlib.sha256()
        for dirpath, dirnames, filenames in os.walk(root):
            # Tests y caches no afectan lo que corre en producción; incluirlos
            # haría que la huella cambie sin motivo operativo.
            dirnames[:] = sorted(
                d for d in dirnames
                if d not in {"__pycache__", "tests", ".pytest_cache", "venv", ".venv"}
            )
            for name in sorted(filenames):
                if not name.endswith(".py"):
                    continue
                path = os.path.join(dirpath, name)
                digest.update(os.path.relpath(path, root).encode("utf-8"))
                with open(path, "rb") as fh:
                    digest.update(fh.read())
        _CODE_FINGERPRINT = digest.hexdigest()[:16]
    except Exception:
        _CODE_FINGERPRINT = ""
    return _CODE_FINGERPRINT


def init_sentry():
    """Initialize Sentry for ANY GenLy process (API or RQ worker).

    Single source of truth (2026-06-01 UMG-launch hardening): main.py
    used to carry its own inline `sentry_sdk.init(...)` with the rich
    integration list, then call this function — and because the SDK
    keeps only the LAST init, this lighter config silently overrode the
    good one (no release tag, no SQLAlchemy tracing). All integrations
    now live here; main.py and worker.py just call init_sentry().

    Every integration import is guarded so the function stays a no-op
    when a dependency is missing (e.g. rq not installed in a tooling
    venv) — per the module contract, observability must never be the
    reason a process fails to start.
    """
    if not SENTRY_DSN:
        return
    # Guard: a non-Railway host (RAILWAY_ENVIRONMENT unset) must NEVER report as
    # a real deploy environment. Otherwise a laptop or a local test/CI run that
    # picked up a prod SENTRY_DSN pollutes the prod Sentry feed with false alarms
    # — exactly what happened 2026-06-03 when a local review run leaked a
    # FileNotFoundError + a provenance FK-violation tagged "production" during the
    # UMG launch, indistinguishable from real incidents. On Railway,
    # RAILWAY_ENVIRONMENT is always injected, so real prod/staging deploys are
    # unaffected. To report from a dev host into a non-prod Sentry environment,
    # set SENTRY_ENV=local (or development) explicitly.
    _resolved_env = (os.environ.get("SENTRY_ENV") or ENV or "").strip().lower()
    if not os.environ.get("RAILWAY_ENVIRONMENT", "").strip() and _resolved_env in ("production", "staging"):
        print(
            f"[OBS] Sentry init SKIPPED: host is not Railway but environment="
            f"{_resolved_env!r}. Refusing to report as a real deploy from a "
            f"local/test host (set SENTRY_ENV=local to report from dev)."
        )
        return
    try:
        import sentry_sdk

        integrations = []
        # API-side integrations: FastAPI/Starlette name every transaction
        # by route template; SQLAlchemy adds child spans per query. See
        # 2026-05-27 Wave-0 observability audit (UMG perf complaint).
        try:
            from sentry_sdk.integrations.fastapi import FastApiIntegration
            from sentry_sdk.integrations.starlette import StarletteIntegration
            integrations.extend([
                FastApiIntegration(transaction_style="endpoint"),
                StarletteIntegration(transaction_style="endpoint"),
            ])
        except ImportError:
            pass
        try:
            from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
            integrations.append(SqlalchemyIntegration())
        except ImportError:
            pass
        # Worker-side integration: RQ tags every job exception with the
        # RQ job id and captures it even when the failure path never
        # reaches our own callbacks (SIGKILL, OOM, serialization errors).
        # Harmless on the API process — it only activates inside an RQ
        # worker loop.
        try:
            from sentry_sdk.integrations.rq import RqIntegration
            integrations.append(RqIntegration())
        except ImportError:
            pass

        sentry_sdk.init(
            dsn=SENTRY_DSN,
            traces_sample_rate=float(os.environ.get("SENTRY_TRACES_RATE", "0.1")),
            # profiles_sample_rate off by default (0.0). Flip to 0.05-0.1
            # temporarily when investigating CPU hotspots — profiling adds
            # measurable overhead to each sampled request.
            profiles_sample_rate=float(os.environ.get("SENTRY_PROFILES_RATE", "0.0")),
            # send_default_pii=False keeps email/IP out of Sentry events
            # (UMG-grade compliance). When debugging specific operators we
            # call sentry_sdk.set_user explicitly to attach just tenant_id.
            send_default_pii=False,
            integrations=integrations,
            # SENTRY_ENV overrides the resolved ENV only if explicitly set
            # (back-compat with the pre-reconcile main.py behavior).
            environment=os.environ.get("SENTRY_ENV") or ENV,
            release=_resolve_release(),
        )
        print(f"[OBS] Sentry initialized (release={_resolve_release()}, "
              f"integrations={len(integrations)})")
        # Forward urllib3 connection-pool warnings to Sentry as
        # explicit events. Sentry's default logging integration only
        # captures ERROR+ — but "Connection pool is full, discarding
        # connection" is a WARNING that, in practice, signals an
        # imminent prod outage (the May 17 incident: pool exhaustion
        # cascaded to /health timeout in ~10 minutes). Catching it
        # here means the operator sees the alert as soon as the
        # SECOND occurrence within the rate-limit window, before any
        # user hits a timeout.
        _install_pool_warning_alert()
    except ImportError:
        print("[OBS] sentry-sdk not installed; skipping")
    except Exception as e:
        print(f"[OBS] Sentry init failed: {e}")


class _PoolWarningSentryFilter(logging.Filter):
    """Logging filter that mirrors selected WARNING records into Sentry
    as events. Filter returns True so the record continues to its
    normal handlers (stdout JSON), it only adds a side-effect."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno < logging.WARNING:
            return True
        msg = record.getMessage()
        # Only the patterns we've confirmed are operational alerts.
        # Adding more triggers here without thought would spam Sentry.
        if "Connection pool is full" in msg or "connection pool full" in msg.lower():
            try:
                import sentry_sdk
                sentry_sdk.capture_message(
                    f"[pool-saturation] {record.name}: {msg}",
                    level="warning",
                )
            except Exception:
                pass
        return True


def _install_pool_warning_alert() -> None:
    """Attach the filter to urllib3.connectionpool. Idempotent — safe to
    call multiple times (filter dedup is by class identity, but we
    guard with a sentinel to keep logs clean)."""
    target = logging.getLogger("urllib3.connectionpool")
    if any(isinstance(f, _PoolWarningSentryFilter) for f in target.filters):
        return
    target.addFilter(_PoolWarningSentryFilter())
    # urllib3 logger defaults to WARNING; ensure we're not below that
    # by accident (some apps set urllib3 to ERROR to silence noise —
    # that would silence our alert too).
    if target.level > logging.WARNING or target.level == logging.NOTSET:
        target.setLevel(logging.WARNING)


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for attr in ("job_id", "tenant_id", "stage", "duration_ms"):
            if hasattr(record, attr):
                payload[attr] = getattr(record, attr)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


# ── Job context en CADA línea de log (observability 2026-06-10) ──
#
# Autopsia del incidente del mural: las líneas [BG]/[EDIT] del pipeline
# no llevan job_id en el mensaje — filtrar los logs de Railway por job
# era imposible (hubo que reconstruir por ventanas de tiempo entre 3
# renders concurrentes). El formatter de arriba YA soporta job_id como
# attr del record; este contextvar + filter lo inyectan automáticamente
# en todo log emitido dentro de un job, sin tocar miles de call sites.
# `railway logs --filter "<job_id>"` ahora matchea todas sus líneas.
_current_job_id = contextvars.ContextVar("genly_job_id", default=None)


def set_job_log_context(job_id):
    """Marca el job activo del proceso/contexto. Llamar al ENTRAR a cada
    entrypoint de worker (run_pipeline, run_edit_pipeline, transcripción,
    prewarm, bg_preview). RQ corre un job por vez por worker, así que el
    set en el entrypoint siguiente pisa al anterior — no hace falta
    reset explícito (clear_job_log_context existe para los tests)."""
    _current_job_id.set(str(job_id) if job_id else None)


def current_job_log_context() -> str | None:
    """Return the job bound to the current worker/request context."""
    return _current_job_id.get()


def clear_job_log_context():
    _current_job_id.set(None)


class _JobContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "job_id"):
            jid = _current_job_id.get()
            if jid:
                record.job_id = jid
        return True


def init_logging():
    """Reconfigure root logger to emit JSON to stdout. Idempotent."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    # Remove existing handlers to avoid double output
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    handler.addFilter(_JobContextFilter())
    root.addHandler(handler)


# Wallclock-style timestamp captured the first time this module is
# imported. Used by health_snapshot() as a startup grace window: during
# the first STARTUP_GRACE_S seconds the endpoint reports "starting"
# instead of "down" when a dependency is briefly unreachable. Without
# this, Railway's healthcheck (30-90 s window) trips on the first
# /health hit if Postgres hasn't fully accepted its first connection
# yet — the deploy then aborts even though the API would be healthy 5 s
# later.
_PROCESS_START_TS = time.monotonic()
STARTUP_GRACE_S = int(os.environ.get("HEALTH_STARTUP_GRACE_S", "20"))


def _within_startup_grace() -> bool:
    return (time.monotonic() - _PROCESS_START_TS) < STARTUP_GRACE_S


# ─── Reaper heartbeat ──────────────────────────────────────────────────
# El daemon thread del reaper (main.py:_reaper_loop) sweep cada 5 min.
# Si muere silenciosamente (excepción raíz, OOM, deadlock, etc.) los
# jobs trabados se acumulan indefinidamente y nadie se entera. Este
# heartbeat captura el último tick exitoso del reaper para que health
# pueda detectar el silencio.
#
# Flujo:
#   - reaper_loop llama mark_reaper_ok() después de cada `_reap()` que
#     no haya tirado excepción.
#   - health_snapshot() compara `now - _REAPER_LAST_OK_TS` contra umbral.
#   - Si supera 2 × intervalo configurado (default 10 min vs 5 min de
#     loop) → degraded "reaper_stalled".
#
# Por qué `time.monotonic()` y no datetime: monotonic no es afectado por
# adjusts del reloj del sistema. La métrica es "tiempo desde el último
# tick", no "wall clock", así que monotonic es lo correcto.
_REAPER_LAST_OK_TS: float | None = None
# Umbral cuando el reaper se considera "stalled". 2× el intervalo del
# loop (300s) + margen de 100s = 700s. Configurable por env si el
# operador quiere tolerar más latencia (e.g., en staging con tráfico bajo).
REAPER_STALLED_AFTER_S = int(os.environ.get("HEALTH_REAPER_STALLED_AFTER_S", "700"))


def mark_reaper_ok() -> None:
    """Reaper loop llama esto después de cada sweep exitoso.

    Idempotente, thread-safe (asignación atómica de float en CPython).
    No-op si se llama desde fuera del reaper — el thread name es la
    única identificación pero no la chequeamos para no perder
    flexibilidad (los tests también lo usan)."""
    global _REAPER_LAST_OK_TS
    _REAPER_LAST_OK_TS = time.monotonic()


def reaper_seconds_since_last_ok() -> float | None:
    """None si el reaper nunca tickeó (proceso recién arrancó); float
    en segundos si ya tickeó al menos una vez."""
    if _REAPER_LAST_OK_TS is None:
        return None
    return time.monotonic() - _REAPER_LAST_OK_TS


def _fleet_service_name(value: object) -> str:
    compact = "".join(ch for ch in str(value or "").lower() if ch.isalnum())
    if compact == "shortworker":
        return "short_worker"
    if compact == "qualityworker":
        return "quality_worker"
    if compact == "batchshortworker":
        return "batch_short_worker"
    if compact == "batchworker":
        return "batch_worker"
    if compact == "worker":
        return "worker"
    return compact or "unknown"


def _fleet_readiness_config(environment: str | None = None) -> tuple[bool, dict[str, int]]:
    """Durable readiness defaults for shared staging/production fleets."""
    managed = (environment or ENV).strip().lower() in {
        "staging", "stage", "prod", "production",
    }
    strict_default = "1" if managed else "0"
    worker_default = "7" if managed else "0"
    short_default = "3" if managed else "0"
    strict = os.environ.get(
        "FLEET_READINESS_STRICT", strict_default,
    ).strip().lower() in {"1", "true", "yes", "on"}

    def _replicas(name: str, default: str) -> int:
        try:
            return max(int(os.environ.get(name, default) or default), 0)
        except (TypeError, ValueError):
            return int(default)

    expected = {
        "worker": _replicas("EXPECTED_WORKER_REPLICAS", worker_default),
        "short_worker": _replicas(
            "EXPECTED_SHORT_WORKER_REPLICAS", short_default,
        ),
    }
    batch_enabled = os.environ.get(
        "BATCH_CAMPAIGN_ENABLED", "0",
    ).strip().lower() in {"1", "true", "yes", "on"}
    if batch_enabled:
        expected["batch_short_worker"] = _replicas(
            "EXPECTED_BATCH_SHORT_WORKER_REPLICAS", "2" if managed else "0",
        )
        expected["batch_worker"] = _replicas(
            "EXPECTED_BATCH_WORKER_REPLICAS", "2" if managed else "0",
        )
    quality_enabled = os.environ.get(
        "TRANSCRIPTION_QUALITY_QUEUE_ENABLED", "0",
    ).strip().lower() in {"1", "true", "yes", "on"}
    if quality_enabled:
        expected["quality_worker"] = _replicas(
            "EXPECTED_QUALITY_WORKER_REPLICAS", "1",
        )
    return strict, expected


def worker_fleet_coherence(
    release_rows: list[dict],
    api_release: str,
    api_protocol: int,
    expected_service_counts: dict[str, int] | None = None,
    api_code_fingerprint: str = "",
) -> dict:
    """Pure deploy-gate contract shared by health and focused tests."""
    expected = {"transcription", "bg_preview", "enterprise", "default"}
    if any(
        _fleet_service_name(service) == "quality_worker" and int(count) > 0
        for service, count in (expected_service_counts or {}).items()
    ):
        expected.add("transcription_quality")
    if any(
        _fleet_service_name(service) == "batch_short_worker" and int(count) > 0
        for service, count in (expected_service_counts or {}).items()
    ):
        expected.update({"transcription_batch", "campaign_control"})
    if any(
        _fleet_service_name(service) == "batch_worker" and int(count) > 0
        for service, count in (expected_service_counts or {}).items()
    ):
        expected.add("batch_render")
    advertised = {
        queue for row in release_rows for queue in (row.get("queues") or [])
    }
    # Compatibilidad API↔worker: se compara la HUELLA DEL CÓDIGO DE BACKEND, no
    # el SHA de git. Los workers tienen filtros de path en Railway, así que un
    # commit de sólo-frontend no los redeploya (correcto) pero sí mueve el SHA
    # de la API — y con la comparación por SHA eso dejaba /health en `down`
    # después de cada merge de frontend, con todo funcionando. Una alarma que
    # grita en falso de rutina enseña a ignorarla.
    #
    # Sólo se usa la huella cuando LOS DOS lados la publican. Un worker viejo
    # (mid-deploy, antes de que este código llegue) no la trae, y ahí se cae al
    # SHA: el comportamiento de siempre, sin ventana ciega durante el rollout.
    api_fingerprint = (api_code_fingerprint or "").strip()

    def _row_matches(row: dict) -> bool:
        row_fp = (row.get("code_fingerprint") or "").strip()
        if api_fingerprint and row_fp:
            return row_fp == api_fingerprint
        return (
            not api_release
            or api_release == "unknown"
            or row.get("release") == api_release
        )

    release_match = bool(release_rows) and all(_row_matches(row) for row in release_rows)
    protocol_match = bool(release_rows) and all(
        row.get("rq_payload_version") == api_protocol for row in release_rows
    )
    service_counts: dict[str, int] = {}
    for row in release_rows:
        service = _fleet_service_name(row.get("service"))
        service_counts[service] = service_counts.get(service, 0) + 1
    normalized_expected = {
        _fleet_service_name(service): max(int(count), 0)
        for service, count in (expected_service_counts or {}).items()
        if int(count) > 0
    }
    under_replicated = {
        service: {
            "expected": expected_count,
            "actual": service_counts.get(service, 0),
        }
        for service, expected_count in normalized_expected.items()
        if service_counts.get(service, 0) < expected_count
    }
    missing = sorted(expected - advertised)
    return {
        "coherent": bool(
            release_match
            and protocol_match
            and not missing
            and not under_replicated
        ),
        "missing_queues": missing,
        "release_match": release_match,
        "protocol_match": protocol_match,
        "service_counts": service_counts,
        "under_replicated": under_replicated,
    }


def health_snapshot(*, enforce_fleet_readiness: bool = True) -> dict:
    """Lightweight report of runtime health.

    Designed to be safe on a hot path (uptime probe → every N seconds):
    every check has its own try/except, every external call has a hard
    timeout, and worst-case the endpoint still returns in well under a
    second.

    Status semantics — used by the load balancer / Docker healthcheck:
      - "ok": all configured dependencies reachable.
      - "starting": we're within the startup grace window AND a
        required dependency is briefly unreachable. Returned as 200 by
        /health so Railway's first healthcheck attempt doesn't roll
        back a deploy that's still warming up.
      - "degraded": a non-critical issue (low disk, no live workers,
        Redis not configured outside prod, etc.) but service is usable.
      - "down": a configured-and-required dependency is unreachable in
        production — Redis (queue is broken) or Postgres (SELECT 1
        failed). The /health endpoint translates this to HTTP 503.
    """
    snap = {
        "status": "ok",
        "env": ENV,
        "release": _resolve_release(),
        "code_fingerprint": backend_code_fingerprint(),
        "features": {
            "anchor_lyrics": os.environ.get("ANCHOR_LYRICS_ENABLED", "0").strip().lower()
            in ("1", "true", "yes", "on"),
            "youtube_publish": os.environ.get("YOUTUBE_PUBLISH_ENABLED", "0").strip().lower()
            in ("1", "true", "yes", "on"),
        },
    }
    try:
        from queue_jobs import RQ_PAYLOAD_VERSION
        snap["rq_payload_version"] = RQ_PAYLOAD_VERSION
    except Exception:
        snap["rq_payload_version"] = int(os.environ.get("RQ_PAYLOAD_VERSION", "2"))
    is_prod = ENV in ("prod", "production", "staging")
    starting = _within_startup_grace()

    def _degrade(reason: str) -> None:
        # First non-fatal problem flips ok→degraded; explicit "down"
        # elsewhere takes precedence.
        if snap["status"] == "ok":
            snap["status"] = "degraded"
            snap["degraded_reason"] = reason

    def _down(reason: str, *, allow_starting: bool = True) -> None:
        # Hard failure of a required dependency. Used by /health to
        # return 503 so the load balancer pulls the instance out.
        # During the startup grace window we report "starting" instead
        # so Railway's first probe doesn't roll back the deploy on a
        # cold-cache miss.
        if starting and allow_starting:
            snap["status"] = "starting"
            snap["starting_reason"] = reason
            return
        snap["status"] = "down"
        snap["down_reason"] = reason

    # Disk
    try:
        du = shutil.disk_usage(os.path.dirname(os.path.abspath(__file__)))
        snap["disk_free_gb"] = round(du.free / 1024 / 1024 / 1024, 1)
        if du.free < 10 * 1024 * 1024 * 1024:  # <10 GB
            _degrade("disk_low")
    except Exception:
        pass

    # Postgres — single SELECT 1, no autocommit, no pool exhaustion.
    try:
        from sqlalchemy import text
        from database import engine
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        snap["db"] = "up"
        # Pool utilization for capacity alerting. checked_out = sockets
        # currently in use; total_capacity = pool_size + max_overflow.
        # Operators alert when checked_out / total > 0.8 (sustained burst).
        try:
            from database import pool_stats
            stats = pool_stats()
            in_use = stats.get("checked_out", 0)
            total = stats.get("total_capacity", 0)
            snap["db_pool"] = {
                "in_use": in_use,
                "total": total,
                "utilization": round(in_use / total, 2) if total else 0.0,
                # Extra detail for at-a-glance debugging when the pool
                # gets tight. None of these are required by the LB.
                "available": stats.get("available", 0),
                "overflow_open": stats.get("overflow", 0),
            }
            if total and in_use / total > 0.8:
                _degrade("db_pool_high")
        except Exception:
            pass
    except Exception as e:
        snap["db"] = "down"
        snap["db_error"] = str(e)[:120]
        _down("db_down")

    # Redis + RQ stats (queue depth, worker count). Best-effort.
    redis_url = os.environ.get("REDIS_URL", "").strip()
    try:
        from queue_jobs import _init_redis
        r, _, _ = _init_redis()
        if r is not None:
            snap["redis"] = "up"
            try:
                from rq import Queue, Worker
                queues = {}
                queue_names = [
                    "transcription", "bg_preview", "enterprise", "default",
                    "transcription_batch", "batch_render", "campaign_control",
                ]
                if os.environ.get(
                    "TRANSCRIPTION_QUALITY_QUEUE_ENABLED", "0"
                ).strip().lower() in {"1", "true", "yes", "on"}:
                    queue_names.append("transcription_quality")
                for qname in queue_names:
                    try:
                        queues[qname] = Queue(qname, connection=r).count
                    except Exception:
                        queues[qname] = -1
                snap["queue_depth"] = queues
                fleet_strict, expected_service_counts = _fleet_readiness_config()
                snap["fleet_readiness_strict"] = fleet_strict
                try:
                    workers = Worker.all(connection=r)
                    snap["workers_alive"] = len(workers)
                except Exception:
                    workers = []
                    snap["workers_alive"] = -1
                if snap.get("workers_alive") == 0:
                    _degrade("no_workers")
                try:
                    release_rows = []
                    for key in r.scan_iter(match="genly:worker:release:*", count=50):
                        raw = r.get(key)
                        if raw:
                            if isinstance(raw, bytes):
                                raw = raw.decode("utf-8", "replace")
                            release_rows.append(json.loads(raw))
                    snap["worker_releases"] = release_rows
                    releases = {row.get("release") for row in release_rows if row.get("release")}
                    protocols = {
                        row.get("rq_payload_version") for row in release_rows
                        if row.get("rq_payload_version") is not None
                    }
                    if len(releases) > 1:
                        _degrade("mixed_worker_releases")
                    if len(protocols) > 1:
                        _degrade("mixed_worker_protocols")
                    fleet = worker_fleet_coherence(
                        release_rows,
                        str(snap.get("release") or ""),
                        int(snap.get("rq_payload_version") or 0),
                        expected_service_counts=expected_service_counts,
                        api_code_fingerprint=backend_code_fingerprint(),
                    )
                    snap["fleet_coherent"] = fleet["coherent"]
                    snap["fleet_missing_queues"] = fleet["missing_queues"]
                    snap["fleet_release_match"] = fleet["release_match"]
                    snap["fleet_protocol_match"] = fleet["protocol_match"]
                    snap["fleet_service_counts"] = fleet["service_counts"]
                    snap["fleet_under_replicated"] = fleet["under_replicated"]
                    if not snap["fleet_coherent"]:
                        if fleet_strict and enforce_fleet_readiness:
                            _down("worker_fleet_incoherent", allow_starting=False)
                        else:
                            _degrade("worker_fleet_incoherent")
                except Exception:
                    snap["worker_releases"] = []
                    snap["fleet_coherent"] = False
                    if fleet_strict and enforce_fleet_readiness:
                        _down("worker_fleet_unverifiable", allow_starting=False)
                    else:
                        _degrade("worker_fleet_unverifiable")
                # Tier 3 segmentation guard: with a segmented fleet (ShortWorker
                # on transcription/bg_preview + Worker on enterprise/default), a
                # rollout mistake (e.g. setting Worker QUEUES=enterprise,default
                # before ShortWorker exists, or ShortWorker dying) leaves a queue
                # with NO consumer. workers_alive stays >0 (the other pool is
                # alive) so "no_workers" never fires and short jobs pile up
                # silently. Degrade if any queue has work but nobody listening.
                try:
                    listened = set()
                    for w in workers:
                        listened.update(w.queue_names())
                    for qname, depth in queues.items():
                        if isinstance(depth, int) and depth > 0 and qname not in listened:
                            _degrade(f"queue_{qname}_no_consumer")
                except Exception:
                    pass
            except Exception:
                # rq not importable in this process — non-fatal for the API
                pass
            # Tier 3b: surface the Veo circuit-breaker state so operators can see
            # when renders are falling to gradient due to a Veo quota event.
            try:
                import veo_breaker
                snap["veo_breaker"] = veo_breaker.state()
            except Exception:
                pass
        elif redis_url:
            # Configured but unreachable: queue is broken. /enqueue will
            # raise in production; surface that to the load balancer.
            snap["redis"] = "down"
            if is_prod:
                _down("redis_down")
            else:
                _degrade("redis_unreachable")
        else:
            snap["redis"] = "not_configured"
            # Only flag as degraded in production — outside prod the
            # threading-based fallback is intentional and the API is
            # fully usable, so the LB shouldn't see "degraded" just
            # because a dev box left REDIS_URL unset.
            if is_prod:
                _degrade("redis_not_configured")
    except Exception:
        snap["redis"] = "error"
        if redis_url and is_prod:
            _down("redis_error")
        else:
            _degrade("redis_error")

    # R2 / S3 — two checks:
    #   1. warmup(): pure CPU, force-loads boto3 model so the first user
    #      request after deploy doesn't pay the 500-1500 ms cold start.
    #   2. probe_r2(): live HEAD via an isolated client (own pool, 2 s
    #      connect / 3 s read). Catches actual R2 outages AND main-pool
    #      saturation that warmup() can't see. Marks degraded if RTT
    #      > 1500 ms (typical R2 head_bucket is 80-200 ms; >1.5 s means
    #      pool churn, retries, or geo-distance issue worth alerting on).
    try:
        import storage
        if storage.is_enabled():
            snap["r2"] = "ready" if storage.warmup() else "configured"
            ok, ms, err = storage.probe_r2()
            snap["r2_probe_ms"] = ms
            snap["r2_circuit_breaker"] = storage.health_probe_state()
            if not ok:
                snap["r2_probe_error"] = err
                if is_prod:
                    _down("r2_probe_failed")
                else:
                    _degrade("r2_probe_failed")
            elif ms > 1500:
                _degrade(f"r2_slow_{ms}ms")
        else:
            snap["r2"] = "not_configured"
            if is_prod:
                _down("r2_not_configured")
    except Exception:
        snap["r2"] = "error"
        if is_prod:
            _down("r2_error")

    # ProRes prewarm throttling counters — surfaces when the queue
    # backpressure (PRORES_PREWARM_MAX_QUEUE_DEPTH) is firing.
    try:
        from queue_jobs import (
            prewarm_skipped_total,
            prewarm_enqueued_total,
        )
        snap["prores_prewarm"] = {
            "enqueued_total": prewarm_enqueued_total,
            "skipped_total": prewarm_skipped_total,
        }
    except Exception:
        pass

    # Reaper heartbeat — daemon thread, dies with the container, NO retry.
    # Sin un heartbeat observable, una muerte silenciosa del thread se
    # traduce en jobs trabados sin alerta. Si el último tick fue hace más
    # de REAPER_STALLED_AFTER_S, degraded "reaper_stalled".
    #
    # None (NULL) durante el primer minuto (cold start hace sleep(60)
    # antes del primer sweep) — no degradar ahí para no falso-alertar.
    secs = reaper_seconds_since_last_ok()
    if secs is None:
        # Tolerar 5 min de cold-start (sleep 60s + primer sweep ~30s +
        # margen). Después de eso, si no tickeó es porque el thread no
        # arrancó o murió antes del primer sweep.
        cold_start_grace_s = 300
        if (time.monotonic() - _PROCESS_START_TS) > cold_start_grace_s:
            snap["reaper"] = "never_ticked"
            _degrade("reaper_never_ticked")
        else:
            snap["reaper"] = "cold_start"
    else:
        snap["reaper"] = {
            "seconds_since_last_ok": round(secs, 1),
            "stalled_threshold_s": REAPER_STALLED_AFTER_S,
        }
        if secs > REAPER_STALLED_AFTER_S:
            _degrade(f"reaper_stalled_{int(secs)}s")

    # External API keys — presence only (doesn't probe the API). A 1-RTT
    # probe per service would burn quota and add latency to every uptime
    # poll; "key set" is enough for "is the deployment configured?".
    snap["api_keys"] = {
        "openai": bool(os.environ.get("OPENAI_API_KEY", "").strip()),
        "vertex": bool(os.environ.get("VERTEX_PROJECT", "").strip()),
        "gemini": bool(os.environ.get("GEMINI_API_KEY", "").strip())
                  or bool(os.environ.get("VERTEX_PROJECT", "").strip()),
        # Sentry DSN presence — lets the launch preflight verify error
        # tracking is actually configured on the deployed instance
        # without sending a test event.
        "sentry": bool(SENTRY_DSN),
    }
    try:
        from ops_control import get_submissions_state
        snap["submissions"] = get_submissions_state()
    except Exception:
        snap["submissions"] = {"paused": False, "source": "unavailable"}
    try:
        from ops_metrics import snapshot as ops_metrics_snapshot
        snap["operational_metrics"] = ops_metrics_snapshot()
    except Exception:
        snap["operational_metrics"] = {}
    return snap
