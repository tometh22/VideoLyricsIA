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
    second. `status` is 'ok' / 'degraded' / 'error' so a load balancer
    can decide.
    """
    snap = {"status": "ok", "env": ENV}

    def _degrade(reason: str) -> None:
        # First problem flips ok→degraded; an explicit "error" elsewhere
        # may upgrade it further.
        if snap["status"] == "ok":
            snap["status"] = "degraded"
            snap["degraded_reason"] = reason

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
    except Exception as e:
        snap["db"] = "down"
        snap["status"] = "error"
        snap["db_error"] = str(e)[:120]

    # Redis + RQ stats (queue depth, worker count). Best-effort: if
    # Redis is down we already marked it; just don't blow up here.
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
        else:
            snap["redis"] = "not_configured"
            _degrade("redis_not_configured")
    except Exception:
        snap["redis"] = "error"
        snap["status"] = "error"

    # R2
    try:
        import storage
        snap["r2"] = "configured" if storage.is_enabled() else "not_configured"
    except Exception:
        snap["r2"] = "error"

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
