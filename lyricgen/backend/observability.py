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
    except ImportError:
        print("[OBS] sentry-sdk not installed; skipping")
    except Exception as e:
        print(f"[OBS] Sentry init failed: {e}")


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


def health_snapshot() -> dict:
    """Lightweight report of runtime health.

    Designed to be safe on a hot path (uptime probe → every N seconds):
    every check has its own try/except, every external call has a hard
    timeout, and worst-case the endpoint still returns in well under a
    second.

    Status semantics — used by the load balancer / Docker healthcheck:
      - "ok": all configured dependencies reachable.
      - "degraded": a non-critical issue (low disk, no live workers,
        Redis not configured outside prod, etc.) but service is usable.
      - "down": a configured-and-required dependency is unreachable in
        production — Redis (queue is broken) or Postgres (SELECT 1
        failed). The /health endpoint translates this to HTTP 503.
    """
    snap = {"status": "ok", "env": ENV}
    is_prod = ENV in ("prod", "production")

    def _degrade(reason: str) -> None:
        # First non-fatal problem flips ok→degraded; explicit "down"
        # elsewhere takes precedence.
        if snap["status"] == "ok":
            snap["status"] = "degraded"
            snap["degraded_reason"] = reason

    def _down(reason: str) -> None:
        # Hard failure of a required dependency. Used by /health to
        # return 503 so the load balancer pulls the instance out.
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

    # R2 / S3 — `warmup()` force-initializes the boto3 client so the
    # first user request after a deploy doesn't pay the cold-start cost
    # (the boto3 model loader is ~500-1500 ms on a fresh process). Pure
    # CPU, no network — safe to run on the hot path.
    try:
        import storage
        if storage.is_enabled():
            snap["r2"] = "ready" if storage.warmup() else "configured"
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
