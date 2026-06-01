"""UMG-launch readiness checks (2026-06-01).

Seven checks that answer "can 3 Universal operators use the platform
simultaneously, right now, without hitting an avoidable wall?":

  launch_health      (P0)  /health green AND enough live workers.
  r2_not_public      (P0)  the R2 bucket rejects unauthenticated access.
  sentry_configured  (P0)  error tracking is live on the deployed instance.
  umg_users_ready    (P0)  the operators' accounts exist, are active, and
                           have quota headroom.
  queue_healthy      (P0)  RQ queues are drained / not backed up.
  presigned_expiry         download URLs carry a bounded expiry.
  limits_sane              backlog/volume/rate limits are at demo-safe values.

All checks are READ-ONLY (safe against prod). Checks that need
credentials or DB access degrade to SKIPPED with a clear reason instead
of failing, so a partial run still yields a useful report.

Env vars consumed:
  PRODUCTION_API_URL          base URL (or --api-url)
  PREFLIGHT_USERNAME/PASSWORD account for authenticated checks (admin for
                              queue_healthy)
  EXPECTED_WORKERS            minimum live worker count (default 7)
  UMG_USERNAMES               comma-separated operator usernames to audit
  UMG_MIN_REMAINING           minimum monthly quota headroom (default 10)
  DATABASE_URL                optional — enables the per-user quota audit
  R2_PROBE_URL                optional — any presigned R2 URL; the check
                              strips the signature and probes anonymously
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests

from ._base import Check, CheckResult


DEFAULT_EXPECTED_WORKERS = 7
DEFAULT_UMG_MIN_REMAINING = 10
HTTP_TIMEOUT = 15


def _get_json(url: str, headers: dict | None = None) -> tuple[int, dict]:
    """GET a JSON endpoint, returning (status_code, payload). Raises on
    network errors so the runner's execute() marks the check as ERROR."""
    r = requests.get(url, headers=headers or {}, timeout=HTTP_TIMEOUT)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {"_raw": r.text[:500]}


def _login(base_url: str, username: str, password: str) -> str:
    """Login and return a JWT. Raises requests.HTTPError on bad creds."""
    r = requests.post(
        f"{base_url}/auth/login",
        json={"username": username, "password": password},
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()["token"]


# ---------------------------------------------------------------------------
# 1. launch_health
# ---------------------------------------------------------------------------

class LaunchHealthCheck(Check):
    name = "launch_health"
    description = ("/health green + reaper alive + enough live workers for "
                   "concurrent operators")
    p0 = True

    def __init__(self, base_url: str, expected_workers: int | None = None):
        self.base_url = base_url.rstrip("/")
        self.expected_workers = expected_workers or int(
            os.environ.get("EXPECTED_WORKERS", str(DEFAULT_EXPECTED_WORKERS))
        )

    def run(self) -> CheckResult:
        code, health = _get_json(f"{self.base_url}/health")
        problems: list[str] = []

        if code != 200:
            return self._failed(
                f"/health returned HTTP {code}", health=health,
            )
        if health.get("status") != "ok":
            problems.append(f"status={health.get('status')!r} (expected 'ok')")
        if health.get("db") != "up":
            problems.append(f"db={health.get('db')!r}")
        if health.get("redis") != "up":
            problems.append(f"redis={health.get('redis')!r} — queue is broken")
        if health.get("r2") not in ("ready", "configured"):
            problems.append(f"r2={health.get('r2')!r} — uploads will fail")

        # Reaper: a dict means it has ticked; "cold_start" is acceptable
        # right after a deploy; "never_ticked" means stuck jobs would
        # accumulate silently.
        reaper = health.get("reaper")
        if reaper == "never_ticked":
            problems.append("reaper never ticked — stuck jobs won't be cleaned up")
        elif isinstance(reaper, dict):
            since = reaper.get("seconds_since_last_ok", 0)
            threshold = reaper.get("stalled_threshold_s", 700)
            if since > threshold:
                problems.append(f"reaper stalled ({since:.0f}s since last sweep)")

        # Worker capacity: 3 concurrent operators × (transcribe + render +
        # bg_preview) need real parallelism. workers_alive == -1 means the
        # API couldn't introspect RQ; treat as a warning, not a fail.
        workers = health.get("workers_alive")
        if workers is None:
            problems.append("workers_alive missing from /health")
        elif workers == 0:
            problems.append("0 live workers — nothing will process jobs")
        elif workers != -1 and workers < self.expected_workers:
            problems.append(
                f"only {workers} live worker(s), expected >= {self.expected_workers} "
                f"(set EXPECTED_WORKERS to adjust)"
            )

        if problems:
            return self._failed(
                f"{len(problems)} issue(s): " + "; ".join(problems),
                health=health, expected_workers=self.expected_workers,
            )
        return self._passed(
            f"health OK — {workers} workers, reaper alive, db/redis/r2 up",
            health=health, expected_workers=self.expected_workers,
        )


# ---------------------------------------------------------------------------
# 2. r2_not_public
# ---------------------------------------------------------------------------

class R2PublicAccessCheck(Check):
    name = "r2_not_public"
    description = ("the R2 bucket rejects unauthenticated GET and LIST — "
                   "label masters are not exposed")
    p0 = True

    def __init__(self, probe_url: str | None = None):
        # Any presigned R2 URL works as a probe seed: we strip the signature
        # query params and verify the bare object/bucket URLs are rejected.
        self.probe_url = (probe_url or os.environ.get("R2_PROBE_URL", "")).strip()

    def run(self) -> CheckResult:
        if not self.probe_url:
            return self._skipped(
                "R2_PROBE_URL not set — paste any presigned R2 URL (e.g. from "
                "a download redirect) to enable the public-access probe"
            )

        parts = urlsplit(self.probe_url)
        if not parts.scheme or not parts.netloc:
            return self._failed(f"R2_PROBE_URL is not a valid URL: {self.probe_url!r}")

        # Bare object URL = same path, signature stripped.
        bare_object_url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        # Bucket root = first path segment only (R2 S3-API URLs are
        # /<bucket>/<key...>); also probe the account root.
        path_segments = [s for s in parts.path.split("/") if s]
        bucket_root_url = urlunsplit((
            parts.scheme, parts.netloc,
            f"/{path_segments[0]}/" if path_segments else "/",
            "", "",
        ))

        leaks: list[str] = []
        details: dict = {"bare_object_url": bare_object_url, "bucket_root_url": bucket_root_url}

        # Unauthenticated GET on the object must be rejected.
        obj = requests.get(bare_object_url, timeout=HTTP_TIMEOUT, stream=True)
        details["object_status"] = obj.status_code
        if obj.status_code == 200:
            leaks.append(
                f"unauthenticated GET on {bare_object_url} returned 200 — "
                "objects are publicly readable"
            )
        obj.close()

        # Unauthenticated LIST on the bucket must be rejected (200 + XML
        # ListBucketResult = the whole catalog is enumerable).
        lst = requests.get(bucket_root_url, timeout=HTTP_TIMEOUT)
        details["list_status"] = lst.status_code
        if lst.status_code == 200 and "ListBucketResult" in lst.text[:2000]:
            leaks.append(
                f"unauthenticated LIST on {bucket_root_url} returned a bucket "
                "listing — all object keys are enumerable"
            )

        if leaks:
            return self._failed(
                "R2 BUCKET IS PUBLICLY ACCESSIBLE: " + "; ".join(leaks),
                **details,
            )
        return self._passed(
            f"bucket rejects anonymous access (object={details['object_status']}, "
            f"list={details['list_status']})",
            **details,
        )


# ---------------------------------------------------------------------------
# 3. sentry_configured
# ---------------------------------------------------------------------------

class SentryConfiguredCheck(Check):
    name = "sentry_configured"
    description = "the deployed instance has a Sentry DSN configured (error tracking live)"
    p0 = True

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def run(self) -> CheckResult:
        code, health = _get_json(f"{self.base_url}/health")
        if code != 200:
            return self._failed(f"/health returned HTTP {code}", health=health)

        api_keys = health.get("api_keys") or {}
        if "sentry" not in api_keys:
            return self._warned(
                "deployed backend does not expose api_keys.sentry — deploy the "
                "observability-hardening PR first, then re-run",
                api_keys=api_keys,
            )
        if not api_keys["sentry"]:
            return self._failed(
                "SENTRY_DSN is NOT set on the deployed instance — errors during "
                "the launch will be invisible",
                api_keys=api_keys,
            )
        return self._passed("Sentry DSN configured on the deployed instance")


# ---------------------------------------------------------------------------
# 4. umg_users_ready
# ---------------------------------------------------------------------------

class UmgUsersCheck(Check):
    name = "umg_users_ready"
    description = ("the launch operators' accounts exist, are active, share a "
                   "tenant, and have monthly quota headroom")
    p0 = True

    def __init__(self, usernames: list[str] | None = None,
                 min_remaining: int | None = None):
        raw = os.environ.get("UMG_USERNAMES", "")
        self.usernames = usernames or [u.strip() for u in raw.split(",") if u.strip()]
        self.min_remaining = min_remaining or int(
            os.environ.get("UMG_MIN_REMAINING", str(DEFAULT_UMG_MIN_REMAINING))
        )

    def run(self) -> CheckResult:
        if not self.usernames:
            return self._skipped(
                "UMG_USERNAMES not set — pass a comma-separated list of the "
                "launch operators' usernames"
            )
        db_url = os.environ.get("DATABASE_URL", "").strip()
        if not db_url:
            return self._skipped(
                "DATABASE_URL not set — cannot audit user accounts/quota "
                "(same graceful-skip pattern as volume_caps)"
            )

        from sqlalchemy import create_engine, text
        engine = create_engine(db_url, pool_pre_ping=True)

        problems: list[str] = []
        users_report: list[dict] = []
        tenants: set[str] = set()

        with engine.connect() as conn:
            for username in self.usernames:
                row = conn.execute(text(
                    "SELECT id, tenant_id, plan, is_active, allow_overage "
                    "FROM users WHERE username = :u"
                ), {"u": username}).fetchone()

                if row is None:
                    problems.append(f"user {username!r} does not exist")
                    users_report.append({"username": username, "exists": False})
                    continue

                user_id, tenant_id, plan, is_active, allow_overage = row
                tenants.add(tenant_id)

                if not is_active:
                    problems.append(f"user {username!r} is DISABLED")

                # Monthly usage: same formula as auth.get_plan_usage —
                # status='done' AND approved_at >= month start, per tenant.
                used = conn.execute(text(
                    "SELECT COUNT(*) FROM jobs WHERE tenant_id = :t "
                    "AND status = 'done' "
                    "AND approved_at >= date_trunc('month', now())"
                ), {"t": tenant_id}).scalar() or 0

                # Plan limits mirror auth.PLANS.
                plan_limits = {"free": 5, "100": 100, "250": 250,
                               "500": 500, "1000": 1000, "unlimited": 999999}
                limit = plan_limits.get(plan or "free", 5)
                remaining = limit - used

                users_report.append({
                    "username": username, "tenant_id": tenant_id, "plan": plan,
                    "is_active": bool(is_active), "allow_overage": bool(allow_overage),
                    "used_this_month": used, "limit": limit, "remaining": remaining,
                })

                if remaining < self.min_remaining and not allow_overage:
                    problems.append(
                        f"user {username!r} has only {remaining} video(s) left this "
                        f"month (plan={plan}, used={used}/{limit}) and allow_overage "
                        f"is OFF — they will hit a 402 mid-launch"
                    )
                if plan == "free":
                    problems.append(
                        f"user {username!r} is on the FREE plan ({limit} videos/month) "
                        "— almost certainly wrong for a label operator"
                    )

        # All launch operators are expected to share one tenant
        # (multi-operator workspace). Two tenants = they can't see each
        # other's jobs, which breaks the agreed workflow.
        existing = [u for u in users_report if u.get("exists", True)]
        if len(existing) > 1 and len(tenants) > 1:
            problems.append(
                f"operators are split across {len(tenants)} tenants ({sorted(tenants)}) "
                "— they will NOT see each other's jobs (expected: one shared tenant)"
            )

        if problems:
            return self._failed(
                f"{len(problems)} account issue(s): " + "; ".join(problems),
                users=users_report, min_remaining=self.min_remaining,
            )
        return self._passed(
            f"{len(self.usernames)} operator account(s) ready "
            f"(tenant={next(iter(tenants), '?')!r}, all active, quota headroom OK)",
            users=users_report, min_remaining=self.min_remaining,
        )


# ---------------------------------------------------------------------------
# 5. queue_healthy
# ---------------------------------------------------------------------------

class QueueHealthCheck(Check):
    name = "queue_healthy"
    description = "RQ queues are drained — no backlog that would delay the first upload"
    p0 = True

    # Anything above this many queued jobs at launch time means the first
    # operator upload waits behind a backlog.
    MAX_QUEUED = 5

    def __init__(self, base_url: str, username: str | None = None,
                 password: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.username = username or os.environ.get("PREFLIGHT_USERNAME")
        self.password = password or os.environ.get("PREFLIGHT_PASSWORD")

    def run(self) -> CheckResult:
        if not (self.username and self.password):
            return self._skipped(
                "PREFLIGHT_USERNAME / PREFLIGHT_PASSWORD not set — cannot call "
                "/admin/queue (admin account required)"
            )
        try:
            token = _login(self.base_url, self.username, self.password)
        except requests.HTTPError as e:
            return self._failed(f"login failed: {e}")

        code, depth = _get_json(
            f"{self.base_url}/admin/queue",
            headers={"Authorization": f"Bearer {token}"},
        )
        if code == 403:
            return self._skipped(
                "PREFLIGHT account is not an admin — /admin/queue requires "
                "role=admin"
            )
        if code != 200:
            return self._failed(f"/admin/queue returned HTTP {code}", response=depth)

        problems: list[str] = []
        for queue_name in ("enterprise", "default", "transcription", "bg_preview"):
            n = depth.get(queue_name)
            if isinstance(n, int) and n > self.MAX_QUEUED:
                problems.append(f"{queue_name} queue has {n} jobs waiting")

        if depth.get("backend") == "threads":
            problems.append(
                "queue backend is 'threads' (Redis not connected) — production "
                "must run on Redis"
            )

        if problems:
            return self._failed("; ".join(problems), queue=depth)
        return self._passed(
            "queues drained "
            + ", ".join(f"{k}={v}" for k, v in depth.items() if isinstance(v, int)),
            queue=depth,
        )


# ---------------------------------------------------------------------------
# 6. presigned_expiry
# ---------------------------------------------------------------------------

class PresignedExpiryCheck(Check):
    name = "presigned_expiry"
    description = "download URLs are presigned with a bounded expiry (no eternal links)"
    p0 = False

    MAX_EXPIRY_SECONDS = 3600 * 24 * 8  # generous ceiling (delivery portal uses 7d)

    def __init__(self, base_url: str, username: str | None = None,
                 password: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.username = username or os.environ.get("PREFLIGHT_USERNAME")
        self.password = password or os.environ.get("PREFLIGHT_PASSWORD")

    def run(self) -> CheckResult:
        if not (self.username and self.password):
            return self._skipped(
                "PREFLIGHT_USERNAME / PREFLIGHT_PASSWORD not set — cannot "
                "exercise the download flow"
            )
        try:
            token = _login(self.base_url, self.username, self.password)
        except requests.HTTPError as e:
            return self._failed(f"login failed: {e}")

        headers = {"Authorization": f"Bearer {token}"}

        # Find a finished job with a video to download.
        code, jobs = _get_json(f"{self.base_url}/jobs", headers=headers)
        if code != 200 or not isinstance(jobs, list):
            return self._failed(f"/jobs returned HTTP {code}")
        done = [j for j in jobs if j.get("status") == "done"]
        if not done:
            return self._skipped(
                "no finished jobs in this account — run one job first or use "
                "an account with history"
            )
        job_id = done[0]["job_id"]

        # Mint a media token, then capture the redirect WITHOUT following it.
        code, mt = _get_json(
            f"{self.base_url}/media-token/{job_id}/video", headers=headers,
        )
        if code != 200:
            return self._failed(f"/media-token returned HTTP {code}", response=mt)

        r = requests.get(
            f"{self.base_url}/download/{job_id}/video",
            params={"token": mt["token"]},
            allow_redirects=False,
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code != 302:
            return self._skipped(
                f"/download did not redirect (HTTP {r.status_code}) — job may be "
                "served from local disk; presign check needs an R2-stored job"
            )

        location = r.headers.get("Location", "")
        m = re.search(r"X-Amz-Expires=(\d+)", location)
        if not m:
            return self._failed(
                "download redirect URL has no X-Amz-Expires — the link never "
                "expires (label masters must not get eternal URLs)",
                location=location[:200],
            )
        expires = int(m.group(1))
        if expires > self.MAX_EXPIRY_SECONDS:
            return self._failed(
                f"presigned URL expiry is {expires}s (> {self.MAX_EXPIRY_SECONDS}s ceiling)",
                expires=expires,
            )
        return self._passed(
            f"download URLs expire after {expires}s", expires=expires, job_id=job_id,
        )


# ---------------------------------------------------------------------------
# 7. limits_sane
# ---------------------------------------------------------------------------

class LimitsConfiguredCheck(Check):
    name = "limits_sane"
    description = ("backlog/volume/rate-limit code defaults are at demo-safe values "
                   "(static scrape of main.py)")
    p0 = False

    # (constant name, expected default, why it matters for the launch)
    EXPECTED = [
        ("USER_BACKLOG_LIMIT", 5,
         "an operator queuing a 6th job mid-demo gets a 429"),
        ("TENANT_BACKLOG_LIMIT", 25,
         "the whole label shares this in-flight ceiling"),
        ("DAILY_VOLUME_CAP", 500,
         "global daily safety valve"),
    ]

    def _read_main(self) -> str:
        return Path(__file__).resolve().parents[2].joinpath("main.py").read_text()

    def run(self) -> CheckResult:
        src = self._read_main()
        report: dict = {}
        problems: list[str] = []

        for name, expected, why in self.EXPECTED:
            # Both forms exist in main.py:
            #   NAME = int(os.environ.get("ENV_NAME", "5"))
            #   NAME = 5
            m = re.search(
                rf'^{re.escape(name)}\s*=\s*int\(os\.environ\.get\("[A-Z_]+",\s*"?(\d+)"?\)\)',
                src, re.MULTILINE,
            ) or re.search(
                rf'"{re.escape(name)}",\s*"(\d+)"', src,
            ) or re.search(
                rf"^{re.escape(name)}\s*=\s*(\d+)\b", src, re.MULTILINE,
            )
            if not m:
                # TENANT_BACKLOG_LIMIT defaults to USER_BACKLOG_LIMIT * 5 —
                # derived defaults are fine, just report them.
                report[name] = "derived/not-found"
                continue
            value = int(m.group(1))
            report[name] = value
            if value < expected:
                problems.append(
                    f"{name} default is {value} (expected >= {expected}) — {why}"
                )

        # Rate limit still present and at the documented value.
        rate_m = re.search(r'default_limits=\["(\d+)/minute"\]', src)
        if rate_m:
            report["rate_limit_per_minute"] = int(rate_m.group(1))
            if int(rate_m.group(1)) < 60:
                problems.append(
                    f"global rate limit is {rate_m.group(1)}/min — the dashboard's "
                    "polling alone can hit this with 3 concurrent operators"
                )
        else:
            problems.append("could not locate the global rate limit in main.py")

        if problems:
            return self._warned("; ".join(problems), **report)
        return self._passed(
            "code defaults are demo-safe: "
            + ", ".join(f"{k}={v}" for k, v in report.items()),
            **report,
        )
