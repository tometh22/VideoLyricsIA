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
ENV = os.environ.get("ENV", "dev").lower()
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
    """Lightweight report of runtime health."""
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
    try:
        from queue_jobs import _init_redis
        r, _, _ = _init_redis()
        snap["redis"] = "up" if r is not None else "not_configured"
    except Exception:
        snap["redis"] = "error"
    # R2
    try:
        import storage
        snap["r2"] = "configured" if storage.is_enabled() else "not_configured"
    except Exception:
        snap["r2"] = "error"
    return snap
