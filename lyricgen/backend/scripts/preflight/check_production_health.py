"""P0 #6 — Production health probe.

Hits the live /health endpoint at the configured PRODUCTION_API_URL (default
https://genly-ai.up.railway.app) and verifies every subsystem the pipeline
depends on is reachable:

  - HTTP 200 within a reasonable timeout
  - env reported as one of {prod, production} (not "dev" — wrong service
    deployed?). Railway uses "production" as ENV; the older deploys used
    "prod". Both are accepted.
  - redis: "up" (worker queue would be a black hole otherwise)
  - r2: "ready" or "configured" (both mean uploads will work — "ready"
    is post-warmup, "configured" is pre-warmup; "not_configured" and
    "error" both fail the check).
  - disk_free_gb above a floor (running out of disk silently kills jobs
    mid-render with cryptic ffmpeg errors)

This catches the "infra is down but everyone assumes it's up" failure mode
without requiring the runner to have credentials.
"""

from __future__ import annotations

import os
from urllib.request import Request, urlopen
import json

from ._base import Check, CheckResult


DEFAULT_PROD_URL = "https://genly-ai.up.railway.app"
MIN_DISK_FREE_GB = 5.0

# Acceptable values from /health for an "in-rotation production deploy".
# observability.health_snapshot already treats both env strings as prod
# (is_prod = ENV in ("prod", "production")), and reports r2 as "ready"
# after the post-warmup probe succeeds — "configured" remains for the
# brief window before warmup, also healthy.
_OK_ENVS = {"prod", "production"}
_OK_R2 = {"ready", "configured"}


class ProductionHealthCheck(Check):
    name = "production_health"
    description = "live /health probe — API up, Redis up, R2 configured, disk healthy"
    p0 = True

    def __init__(self, base_url: str | None = None):
        self.base_url = (
            base_url or os.environ.get("PRODUCTION_API_URL", DEFAULT_PROD_URL)
        ).rstrip("/")

    def run(self) -> CheckResult:
        url = f"{self.base_url}/health"
        try:
            with urlopen(Request(url), timeout=10) as resp:
                code = resp.getcode()
                body = resp.read().decode()
        except Exception as e:
            return self._failed(
                f"could not reach {url}: {type(e).__name__}: {e}",
                url=url,
            )

        if code != 200:
            return self._failed(
                f"/health returned HTTP {code}",
                url=url, body=body[:500],
            )

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return self._failed(
                "/health did not return JSON — wrong service hit?",
                url=url, body=body[:500],
            )

        problems: list[str] = []

        if payload.get("status") != "ok":
            problems.append(f"status is {payload.get('status')!r}, expected 'ok'")
        if payload.get("env") not in _OK_ENVS:
            problems.append(
                f"env is {payload.get('env')!r}, expected one of {sorted(_OK_ENVS)} "
                "— wrong service deployed?"
            )
        if payload.get("redis") != "up":
            problems.append(
                f"redis is {payload.get('redis')!r}, expected 'up' "
                "— worker queue is broken"
            )
        if payload.get("r2") not in _OK_R2:
            problems.append(
                f"r2 is {payload.get('r2')!r}, expected one of {sorted(_OK_R2)} "
                "— uploads will 500"
            )
        disk_free = payload.get("disk_free_gb", 0)
        if disk_free < MIN_DISK_FREE_GB:
            problems.append(
                f"disk_free_gb is {disk_free}, below floor {MIN_DISK_FREE_GB}"
            )

        if problems:
            return self._failed(
                f"{len(problems)} subsystem issue(s) — pipeline cannot run reliably",
                url=url,
                health=payload,
                violations=problems,
            )

        return self._passed(
            f"production /health OK (env={payload.get('env')!r}, redis up, "
            f"r2={payload.get('r2')!r}, {disk_free} GB free)",
            url=url,
            health=payload,
        )
