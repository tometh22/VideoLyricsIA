"""UMG-launch readiness checks — self-tests (scripts/preflight/check_launch_readiness.py).

These verify the CHECK LOGIC, not the deployed infra: every HTTP/DB call is
mocked. The contract pinned here:

  - A green environment → PASS.
  - Each specific degradation (no workers, public bucket, missing Sentry,
    queue backlog) → FAIL with an actionable summary.
  - Missing credentials/config → SKIPPED, never a false FAIL.
  - The Makefile entry point (--launch flag → LAUNCH_CHECKS) stays in sync
    with the actual check names.
"""

from types import SimpleNamespace

from scripts.preflight._base import Status
from scripts.preflight import check_launch_readiness as clr
from scripts.preflight.check_launch_readiness import (
    LaunchHealthCheck,
    LimitsConfiguredCheck,
    PresignedExpiryCheck,
    QueueHealthCheck,
    R2PublicAccessCheck,
    SentryConfiguredCheck,
    UmgUsersCheck,
)


HEALTHY = {
    "status": "ok",
    "env": "production",
    "db": "up",
    "redis": "up",
    "r2": "ready",
    "reaper": {"seconds_since_last_ok": 12.0, "stalled_threshold_s": 700},
    "workers_alive": 7,
    "queue_depth": {"enterprise": 0, "default": 0},
    "api_keys": {"openai": True, "vertex": True, "gemini": True, "sentry": True},
}


def _mock_health(monkeypatch, payload, code=200):
    monkeypatch.setattr(clr, "_get_json", lambda url, headers=None: (code, payload))


# ---------------------------------------------------------------------------
# launch_health
# ---------------------------------------------------------------------------

def test_launch_health_passes_on_green_env(monkeypatch):
    _mock_health(monkeypatch, dict(HEALTHY))
    result = LaunchHealthCheck("https://api.example", expected_workers=7).run()
    assert result.status == Status.PASS, result.summary


def test_launch_health_fails_on_too_few_workers(monkeypatch):
    payload = dict(HEALTHY, workers_alive=2)
    _mock_health(monkeypatch, payload)
    result = LaunchHealthCheck("https://api.example", expected_workers=7).run()
    assert result.status == Status.FAIL
    assert "2 live worker" in result.summary


def test_launch_health_fails_on_dead_reaper(monkeypatch):
    payload = dict(HEALTHY, reaper="never_ticked")
    _mock_health(monkeypatch, payload)
    result = LaunchHealthCheck("https://api.example", expected_workers=7).run()
    assert result.status == Status.FAIL
    assert "reaper" in result.summary.lower()


def test_launch_health_fails_on_stalled_reaper(monkeypatch):
    payload = dict(HEALTHY, reaper={"seconds_since_last_ok": 9999.0,
                                    "stalled_threshold_s": 700})
    _mock_health(monkeypatch, payload)
    result = LaunchHealthCheck("https://api.example", expected_workers=7).run()
    assert result.status == Status.FAIL
    assert "stalled" in result.summary.lower()


def test_launch_health_fails_on_redis_down(monkeypatch):
    payload = dict(HEALTHY, redis="down", status="down")
    _mock_health(monkeypatch, payload)
    result = LaunchHealthCheck("https://api.example", expected_workers=7).run()
    assert result.status == Status.FAIL


def test_launch_health_fails_on_http_error(monkeypatch):
    _mock_health(monkeypatch, {"detail": "boom"}, code=503)
    result = LaunchHealthCheck("https://api.example", expected_workers=7).run()
    assert result.status == Status.FAIL
    assert "503" in result.summary


# ---------------------------------------------------------------------------
# r2_not_public
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text

    def close(self):
        pass


def test_r2_check_skips_without_probe_url(monkeypatch):
    monkeypatch.delenv("R2_PROBE_URL", raising=False)
    result = R2PublicAccessCheck(probe_url=None).run()
    assert result.status == Status.SKIPPED


def test_r2_check_passes_when_bucket_rejects_anonymous(monkeypatch):
    monkeypatch.setattr(clr.requests, "get",
                        lambda url, **kw: _FakeResponse(403, "AccessDenied"))
    result = R2PublicAccessCheck(
        probe_url="https://acc.r2.cloudflarestorage.com/genly/inputs/t/j/song.mp3"
                  "?X-Amz-Signature=abc&X-Amz-Expires=900"
    ).run()
    assert result.status == Status.PASS, result.summary


def test_r2_check_fails_loudly_when_object_is_public(monkeypatch):
    monkeypatch.setattr(clr.requests, "get",
                        lambda url, **kw: _FakeResponse(200, "binarycontent"))
    result = R2PublicAccessCheck(
        probe_url="https://acc.r2.cloudflarestorage.com/genly/inputs/t/j/song.mp3"
                  "?X-Amz-Signature=abc"
    ).run()
    assert result.status == Status.FAIL
    assert "PUBLICLY ACCESSIBLE" in result.summary


def test_r2_check_fails_when_bucket_is_listable(monkeypatch):
    def _fake_get(url, **kw):
        # Object GET rejected, but bucket LIST returns an S3 listing.
        if url.endswith("/genly/"):
            return _FakeResponse(200, "<?xml ?><ListBucketResult>...</ListBucketResult>")
        return _FakeResponse(403, "AccessDenied")

    monkeypatch.setattr(clr.requests, "get", _fake_get)
    result = R2PublicAccessCheck(
        probe_url="https://acc.r2.cloudflarestorage.com/genly/inputs/t/j/song.mp3"
                  "?X-Amz-Signature=abc"
    ).run()
    assert result.status == Status.FAIL
    assert "enumerable" in result.summary


# ---------------------------------------------------------------------------
# sentry_configured
# ---------------------------------------------------------------------------

def test_sentry_check_passes_when_dsn_set(monkeypatch):
    _mock_health(monkeypatch, dict(HEALTHY))
    result = SentryConfiguredCheck("https://api.example").run()
    assert result.status == Status.PASS


def test_sentry_check_fails_when_dsn_missing(monkeypatch):
    payload = dict(HEALTHY, api_keys={**HEALTHY["api_keys"], "sentry": False})
    _mock_health(monkeypatch, payload)
    result = SentryConfiguredCheck("https://api.example").run()
    assert result.status == Status.FAIL


def test_sentry_check_warns_on_old_backend_without_field(monkeypatch):
    """A backend deployed before the observability-hardening PR doesn't
    expose api_keys.sentry — that's a WARN (deploy ordering issue), not
    a FAIL (we genuinely don't know)."""
    payload = dict(HEALTHY, api_keys={"openai": True, "vertex": True, "gemini": True})
    _mock_health(monkeypatch, payload)
    result = SentryConfiguredCheck("https://api.example").run()
    assert result.status == Status.WARN


# ---------------------------------------------------------------------------
# umg_users_ready
# ---------------------------------------------------------------------------

def test_umg_users_check_skips_without_usernames(monkeypatch):
    monkeypatch.delenv("UMG_USERNAMES", raising=False)
    result = UmgUsersCheck(usernames=None).run()
    assert result.status == Status.SKIPPED


def test_umg_users_check_skips_without_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    result = UmgUsersCheck(usernames=["op1", "op2", "op3"]).run()
    assert result.status == Status.SKIPPED
    assert "DATABASE_URL" in result.summary


# ---------------------------------------------------------------------------
# queue_healthy
# ---------------------------------------------------------------------------

def test_queue_check_skips_without_credentials(monkeypatch):
    monkeypatch.delenv("PREFLIGHT_USERNAME", raising=False)
    monkeypatch.delenv("PREFLIGHT_PASSWORD", raising=False)
    result = QueueHealthCheck("https://api.example").run()
    assert result.status == Status.SKIPPED


def test_queue_check_passes_on_drained_queues(monkeypatch):
    monkeypatch.setattr(clr, "_login", lambda *a: "fake-jwt")
    monkeypatch.setattr(clr, "_get_json", lambda url, headers=None: (
        200, {"enterprise": 0, "default": 1, "backend": "redis"},
    ))
    result = QueueHealthCheck("https://api.example", "admin", "pw").run()
    assert result.status == Status.PASS, result.summary


def test_queue_check_fails_on_backlog(monkeypatch):
    monkeypatch.setattr(clr, "_login", lambda *a: "fake-jwt")
    monkeypatch.setattr(clr, "_get_json", lambda url, headers=None: (
        200, {"enterprise": 12, "default": 30, "backend": "redis"},
    ))
    result = QueueHealthCheck("https://api.example", "admin", "pw").run()
    assert result.status == Status.FAIL
    assert "12" in result.summary or "30" in result.summary


def test_queue_check_fails_on_threads_backend(monkeypatch):
    """backend='threads' in prod means Redis is not connected — jobs would
    run inside the API process and die on every deploy."""
    monkeypatch.setattr(clr, "_login", lambda *a: "fake-jwt")
    monkeypatch.setattr(clr, "_get_json", lambda url, headers=None: (
        200, {"enterprise": 0, "default": 0, "backend": "threads"},
    ))
    result = QueueHealthCheck("https://api.example", "admin", "pw").run()
    assert result.status == Status.FAIL
    assert "threads" in result.summary


def test_queue_check_skips_for_non_admin_account(monkeypatch):
    monkeypatch.setattr(clr, "_login", lambda *a: "fake-jwt")
    monkeypatch.setattr(clr, "_get_json", lambda url, headers=None: (
        403, {"detail": "Admin only"},
    ))
    result = QueueHealthCheck("https://api.example", "operator", "pw").run()
    assert result.status == Status.SKIPPED


# ---------------------------------------------------------------------------
# presigned_expiry
# ---------------------------------------------------------------------------

def test_presigned_check_skips_without_credentials(monkeypatch):
    monkeypatch.delenv("PREFLIGHT_USERNAME", raising=False)
    monkeypatch.delenv("PREFLIGHT_PASSWORD", raising=False)
    result = PresignedExpiryCheck("https://api.example").run()
    assert result.status == Status.SKIPPED


def test_presigned_check_fails_on_eternal_url(monkeypatch):
    """A redirect Location without X-Amz-Expires means the link never
    expires — exactly what a label must not get."""
    monkeypatch.setattr(clr, "_login", lambda *a: "fake-jwt")
    monkeypatch.setattr(clr, "_get_json", lambda url, headers=None: (
        200,
        [{"job_id": "abc123", "status": "done"}] if url.endswith("/jobs")
        else {"token": "media-jwt"},
    ))
    monkeypatch.setattr(clr.requests, "get", lambda url, **kw: SimpleNamespace(
        status_code=302,
        headers={"Location": "https://r2.example/genly/t/j/video.mp4"},  # no expiry!
    ))
    result = PresignedExpiryCheck("https://api.example", "op", "pw").run()
    assert result.status == Status.FAIL
    assert "expire" in result.summary.lower()


def test_presigned_check_passes_on_bounded_expiry(monkeypatch):
    monkeypatch.setattr(clr, "_login", lambda *a: "fake-jwt")
    monkeypatch.setattr(clr, "_get_json", lambda url, headers=None: (
        200,
        [{"job_id": "abc123", "status": "done"}] if url.endswith("/jobs")
        else {"token": "media-jwt"},
    ))
    monkeypatch.setattr(clr.requests, "get", lambda url, **kw: SimpleNamespace(
        status_code=302,
        headers={"Location": "https://r2.example/v.mp4?X-Amz-Expires=3600&X-Amz-Signature=x"},
    ))
    result = PresignedExpiryCheck("https://api.example", "op", "pw").run()
    assert result.status == Status.PASS
    assert result.details["expires"] == 3600


# ---------------------------------------------------------------------------
# limits_sane — runs against the REAL main.py source (static scrape)
# ---------------------------------------------------------------------------

def test_limits_check_against_real_source():
    """No mocks: scrape the actual main.py. If someone weakens the backlog
    or volume defaults, this turns red in CI."""
    result = LimitsConfiguredCheck().run()
    assert result.status in (Status.PASS, Status.WARN), result.summary
    # The constants must at least be locatable.
    assert result.details.get("USER_BACKLOG_LIMIT") not in (None, "derived/not-found"), (
        "USER_BACKLOG_LIMIT no longer found in main.py — check the scrape regex"
    )
    assert result.details.get("rate_limit_per_minute"), (
        "global rate limit no longer found in main.py"
    )


# ---------------------------------------------------------------------------
# Makefile entry point stays in sync
# ---------------------------------------------------------------------------

def test_launch_checks_list_matches_real_check_names():
    """`make preflight-staging` runs `--launch`, which expands to
    LAUNCH_CHECKS via --only filtering. A typo in either place silently
    runs zero checks — pin the mapping."""
    from scripts.preflight.run import LAUNCH_CHECKS

    real_names = {
        LaunchHealthCheck("https://x").name,
        R2PublicAccessCheck().name,
        SentryConfiguredCheck("https://x").name,
        UmgUsersCheck(usernames=["u"]).name,
        QueueHealthCheck("https://x").name,
        PresignedExpiryCheck("https://x").name,
        LimitsConfiguredCheck().name,
    }
    assert set(LAUNCH_CHECKS) == real_names


def test_all_launch_checks_are_registered_in_runner():
    """build_checks() must include every launch check, otherwise --launch
    filters down to nothing and the report is empty."""
    from scripts.preflight.run import LAUNCH_CHECKS, build_checks

    args = SimpleNamespace(
        api_url="https://api.example",
        umg_master=None, umg_fps=23.976, umg_profile=3,
        validator_prompts=5, validator_budget=5.0,
        concurrency_mp3=None, concurrency_n=3, concurrency_timeout=1500,
        expected_workers=7, r2_probe_url=None,
    )
    registered = {c.name for c in build_checks(args)}
    missing = set(LAUNCH_CHECKS) - registered
    assert not missing, f"launch checks not registered in build_checks(): {missing}"
