"""Sentry release-tag resolution (UMG-launch hardening 2026-06-01).

Without a release tag, Sentry events can't be correlated to the deploy
that introduced them — "did the spike start with this morning's push?"
becomes guesswork. _resolve_release() is the single source of truth for
both the API and the worker process.

Precedence pinned here:
  1. SENTRY_RELEASE      — explicit operator override.
  2. RAILWAY_GIT_COMMIT_SHA — injected by Railway on every build.
  3. "genly@2.0.0"       — static fallback (never untagged).
"""

from observability import _resolve_release


def test_release_prefers_explicit_sentry_release(monkeypatch):
    monkeypatch.setenv("SENTRY_RELEASE", "genly@v123")
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "abc123def456")
    assert _resolve_release() == "genly@v123"


def test_release_falls_back_to_railway_commit_sha(monkeypatch):
    monkeypatch.delenv("SENTRY_RELEASE", raising=False)
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "abc123def456")
    assert _resolve_release() == "abc123def456"


def test_release_static_fallback_when_nothing_set(monkeypatch):
    monkeypatch.delenv("SENTRY_RELEASE", raising=False)
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
    assert _resolve_release() == "genly@2.0.0"


def test_release_ignores_blank_env_values(monkeypatch):
    """Railway sometimes injects empty-string vars on misconfigured
    services — blank must not win over the static fallback."""
    monkeypatch.setenv("SENTRY_RELEASE", "   ")
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "")
    assert _resolve_release() == "genly@2.0.0"


def test_init_sentry_is_noop_without_dsn(monkeypatch):
    """Module contract: observability must never be the reason a process
    fails to start. Without SENTRY_DSN, init_sentry() returns without
    importing or initializing the SDK."""
    import observability
    monkeypatch.setattr(observability, "SENTRY_DSN", "")
    # Must not raise even if sentry_sdk is not importable at all.
    observability.init_sentry()


def test_health_snapshot_reports_sentry_presence(monkeypatch):
    """The launch preflight reads /health → api_keys.sentry to verify the
    deployed instance has error tracking configured. Pin the field."""
    import observability
    monkeypatch.setattr(observability, "SENTRY_DSN", "https://key@sentry.example/1")
    snap = observability.health_snapshot()
    assert snap["api_keys"]["sentry"] is True

    monkeypatch.setattr(observability, "SENTRY_DSN", "")
    snap = observability.health_snapshot()
    assert snap["api_keys"]["sentry"] is False
