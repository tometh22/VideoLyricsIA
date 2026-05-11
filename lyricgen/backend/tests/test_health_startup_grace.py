"""Healthcheck startup grace tests.

The original incident: Railway deploy at 2026-05-11 22:00 UTC failed
with "5/5 replicas never became healthy" because /health returned 503
on the first probe — the SQLAlchemy pool hadn't seated its first
Postgres socket yet, health_snapshot's SELECT 1 fell into _down(),
and the load balancer rolled back the deploy.

Fix: during the first STARTUP_GRACE_S seconds of process lifetime,
mark dependency failures as "starting" instead of "down", so /health
returns 200 and Railway's probe budget is spent waiting for the pool
to warm up, not declaring the deploy dead.

These tests pin the contract:
  • _within_startup_grace() returns True for the first N seconds
  • _down() called during grace produces status="starting" + 200
  • _down() called after grace produces status="down" + 503
  • normal status="ok" path is unchanged
"""
from __future__ import annotations

import importlib
import time


def _reload_observability(grace_seconds: int):
    """Reload observability with a known grace value and reset the
    process-start timestamp. Returns the freshly-imported module so
    each test gets a clean slate."""
    import observability as obs
    obs.STARTUP_GRACE_S = grace_seconds
    obs._PROCESS_START_TS = time.monotonic()
    return obs


def test_within_grace_window_returns_true_immediately():
    obs = _reload_observability(grace_seconds=30)
    assert obs._within_startup_grace() is True, (
        "process just started — should be within grace"
    )


def test_grace_window_expires_after_configured_seconds():
    obs = _reload_observability(grace_seconds=1)
    # Wait past the grace window. 1 s + a small buffer for clock noise.
    time.sleep(1.1)
    assert obs._within_startup_grace() is False, (
        "1 s grace expired — should be False"
    )


def test_db_down_during_grace_window_reports_starting(monkeypatch):
    """During the grace window, a PG SELECT 1 that raises must
    produce status='starting' (200 from /health), NOT 'down' (503)."""
    obs = _reload_observability(grace_seconds=30)
    # Make every PG call fail.
    from sqlalchemy import exc as _sql_exc

    class _BoomConn:
        def __enter__(self): raise _sql_exc.OperationalError("boom", None, Exception("no pg"))
        def __exit__(self, *a): return False

    monkeypatch.setattr(
        "database.engine.connect", lambda: _BoomConn(),
    )
    snap = obs.health_snapshot()
    assert snap.get("status") == "starting", (
        f"during grace, PG-down should report starting, got {snap}"
    )
    assert snap.get("starting_reason") == "db_down"
    # No 'down' key should be set when we're in the starting state.
    assert "down_reason" not in snap


def test_db_down_after_grace_reports_down(monkeypatch):
    """After grace expires, the same PG failure must produce
    status='down' (503 from /health) so the LB pulls the instance."""
    obs = _reload_observability(grace_seconds=1)
    time.sleep(1.1)  # exit the grace window
    from sqlalchemy import exc as _sql_exc

    class _BoomConn:
        def __enter__(self): raise _sql_exc.OperationalError("boom", None, Exception("no pg"))
        def __exit__(self, *a): return False

    monkeypatch.setattr(
        "database.engine.connect", lambda: _BoomConn(),
    )
    snap = obs.health_snapshot()
    assert snap.get("status") == "down", (
        f"after grace, PG-down should report down, got {snap}"
    )
    assert snap.get("down_reason") == "db_down"


def test_healthy_state_unchanged_during_grace():
    """In normal (PG up) operation, the grace window must NOT change
    the reported status. We don't want to mask real problems by
    keeping every fresh container in 'starting' forever."""
    obs = _reload_observability(grace_seconds=30)
    snap = obs.health_snapshot()
    # On the test DB the SELECT 1 should succeed → status stays 'ok'
    # (or 'degraded' if redis is unreachable in test env). The
    # invariant we care about: it does NOT become 'starting' just
    # because we're within the window.
    assert snap.get("status") in ("ok", "degraded"), (
        f"healthy PG should yield ok/degraded, not starting: {snap}"
    )


def test_health_endpoint_returns_200_for_starting_status(monkeypatch, client):
    """End-to-end: /health must return 200 when the snapshot is
    'starting'. Otherwise Railway rolls back the deploy."""
    monkeypatch.setattr(
        "main.health_snapshot",
        lambda: {"status": "starting", "env": "test", "starting_reason": "db_down"},
    )
    r = client.get("/health")
    assert r.status_code == 200, (
        f"/health on status=starting must be 200 (got {r.status_code}) "
        f"so the deploy doesn't roll back"
    )
    assert r.json()["status"] == "starting"


def test_health_endpoint_returns_503_for_down_status(monkeypatch, client):
    """Confirm the other side of the contract: status='down' still
    surfaces 503 so the LB pulls the instance out of rotation."""
    monkeypatch.setattr(
        "main.health_snapshot",
        lambda: {"status": "down", "env": "test", "down_reason": "db_down"},
    )
    r = client.get("/health")
    assert r.status_code == 503
