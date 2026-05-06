"""P0 #6 — Production health probe.

Hits the live /health endpoint at the configured PRODUCTION_API_URL (default
https://genly-ai.up.railway.app) and verifies every subsystem the pipeline
depends on is reachable:

  - HTTP 200 within a reasonable timeout
  - env reported as "prod" (not "dev" — wrong service deployed?)
  - redis: "up" (worker queue would be a black hole otherwise)
  - r2: "configured" (uploads would 500)
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
        if payload.get("env") != "prod":
            problems.append(
                f"env is {payload.get('env')!r}, expected 'prod' "
                "— wrong service deployed?"
            )
        if payload.get("redis") != "up":
            problems.append(
                f"redis is {payload.get('redis')!r}, expected 'up' "
                "— worker queue is broken"
            )
        if payload.get("r2") != "configured":
            problems.append(
                f"r2 is {payload.get('r2')!r}, expected 'configured' "
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
            f"production /health OK (redis up, r2 configured, "
            f"{disk_free} GB free)",
            url=url,
            health=payload,
        )
