"""Observability: Sentry, structured logging, health checks.

All helpers are no-ops when the relevant env vars are missing, so this module
never becomes the reason a local dev session fails to start.
"""

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


def init_sentry():
    if not SENTRY_DSN:
        return
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        sentry_sdk.init(
            dsn=SENTRY_DSN,
            environment=ENV,
            traces_sample_rate=0.1,
            profiles_sample_rate=0.0,
            send_default_pii=False,
            integrations=[FastApiIntegration()],
        )
        print("[OBS] Sentry initialized")
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


def init_logging():
    """Reconfigure root logger to emit JSON to stdout. Idempotent."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    # Remove existing handlers to avoid double output
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
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


def health_snapshot() -> dict:
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
    snap = {"status": "ok", "env": ENV}
    is_prod = ENV in ("prod", "production")
    starting = _within_startup_grace()

    def _degrade(reason: str) -> None:
        # First non-fatal problem flips ok→degraded; explicit "down"
        # elsewhere takes precedence.
        if snap["status"] == "ok":
            snap["status"] = "degraded"
            snap["degraded_reason"] = reason

    def _down(reason: str) -> None:
        # Hard failure of a required dependency. Used by /health to
        # return 503 so the load balancer pulls the instance out.
        # During the startup grace window we report "starting" instead
        # so Railway's first probe doesn't roll back the deploy on a
        # cold-cache miss.
        if starting:
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
                for qname in ("enterprise", "default"):
                    try:
                        queues[qname] = Queue(qname, connection=r).count
                    except Exception:
                        queues[qname] = -1
                snap["queue_depth"] = queues
                try:
                    snap["workers_alive"] = len(Worker.all(connection=r))
                except Exception:
                    snap["workers_alive"] = -1
                if snap.get("workers_alive") == 0:
                    _degrade("no_workers")
            except Exception:
                # rq not importable in this process — non-fatal for the API
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
            if not ok:
                snap["r2_probe_error"] = err
                _degrade("r2_probe_failed")
            elif ms > 1500:
                _degrade(f"r2_slow_{ms}ms")
        else:
            snap["r2"] = "not_configured"
    except Exception:
        snap["r2"] = "error"

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

    # External API keys — presence only (doesn't probe the API). A 1-RTT
    # probe per service would burn quota and add latency to every uptime
    # poll; "key set" is enough for "is the deployment configured?".
    snap["api_keys"] = {
        "openai": bool(os.environ.get("OPENAI_API_KEY", "").strip()),
        "vertex": bool(os.environ.get("VERTEX_PROJECT", "").strip()),
        "gemini": bool(os.environ.get("GEMINI_API_KEY", "").strip())
                  or bool(os.environ.get("VERTEX_PROJECT", "").strip()),
    }
    return snap
