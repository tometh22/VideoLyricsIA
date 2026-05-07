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

    Status semantics — used by the load balancer / Docker healthcheck:
      - "ok": all configured dependencies reachable.
      - "degraded": something is off but the service is partly usable
        (low disk, Redis configured but unreachable when the API has
        threads-fallback available outside prod).
      - "down": a configured-and-required dependency is unreachable
        (Redis configured but ping failed in prod).
    """
    snap = {"status": "ok", "env": ENV}
    # Disk
    try:
        du = shutil.disk_usage(os.path.dirname(os.path.abspath(__file__)))
        snap["disk_free_gb"] = round(du.free / 1024 / 1024 / 1024, 1)
        if du.free < 10 * 1024 * 1024 * 1024:  # <10 GB
            snap["status"] = "degraded"
    except Exception:
        pass
    # Redis
    redis_url = os.environ.get("REDIS_URL", "").strip()
    try:
        from queue_jobs import _init_redis
        r, _, _ = _init_redis()
        if r is not None:
            snap["redis"] = "up"
        elif redis_url:
            # Configured but unreachable: queue is broken. /enqueue will
            # raise in production; surface that to the load balancer.
            snap["redis"] = "down"
            snap["status"] = "down" if ENV in ("prod", "production") else "degraded"
        else:
            snap["redis"] = "not_configured"
    except Exception:
        snap["redis"] = "error"
        if redis_url:
            snap["status"] = "down" if ENV in ("prod", "production") else "degraded"
    # R2 / S3
    try:
        import storage
        snap["r2"] = "configured" if storage.is_enabled() else "not_configured"
    except Exception:
        snap["r2"] = "error"
    return snap
