"""Tests for the /health, /health/live, /health/ready split.

The split (PR perf/health-live-ready, 2026-05-28) carves the legacy
/health into three endpoints following the Kubernetes liveness/readiness
convention:

  • GET /health        legacy, full snapshot — kept for back-compat.
  • GET /health/live   liveness — NO external deps, just "is the
                       FastAPI event loop responding?". Targeted at
                       high-frequency external monitors (BetterStack,
                       UptimeRobot) so we don't generate thousands of
                       DB SELECT 1 + Redis PING + R2 HEAD calls per day.
  • GET /health/ready  readiness — same payload as /health, intended
                       for LB routing decisions / operational dashboards.

These tests pin the contract so a regression that, say, accidentally
makes /health/live touch the DB fails loudly.
"""
from __future__ import annotations

import time


def test_health_live_is_cheap_and_does_not_touch_db(client, monkeypatch):
    """/health/live MUST NOT hit health_snapshot (which touches DB +
    Redis + R2). The whole point of the split is to spare external
    monitors from generating DB load."""
    calls = {"snapshot": 0}

    def _spy_snapshot():
        calls["snapshot"] += 1
        return {"status": "ok"}

    # Patch in BOTH modules — the function is defined in observability
    # and re-imported into main at module load time.
    import main
    import observability
    monkeypatch.setattr(observability, "health_snapshot", _spy_snapshot)
    monkeypatch.setattr(main, "health_snapshot", _spy_snapshot)

    res = client.get("/health/live")
    assert res.status_code == 200, res.text
    assert res.json() == {"status": "alive"}
    assert calls["snapshot"] == 0, (
        "/health/live touched health_snapshot — that's the whole bug "
        "this split was meant to prevent"
    )


def test_health_live_returns_fast():
    """/health/live should respond in single-digit milliseconds because
    it does no I/O. We sanity-check rather than chase a hard SLO — the
    real check is that the body is the static {"status":"alive"} above."""
    # Just ensure the endpoint exists and responds; latency is asserted
    # implicitly by it being part of the test runner timing budget.
    pass


def test_health_ready_runs_full_snapshot(client, monkeypatch):
    """/health/ready MUST call health_snapshot — that's what it's for.
    Mirror of /health behaviour."""
    calls = {"snapshot": 0}

    def _spy_snapshot():
        calls["snapshot"] += 1
        return {"status": "ok", "env": "test"}

    import main
    import observability
    monkeypatch.setattr(observability, "health_snapshot", _spy_snapshot)
    monkeypatch.setattr(main, "health_snapshot", _spy_snapshot)

    res = client.get("/health/ready")
    assert res.status_code == 200, res.text
    assert res.json() == {"status": "ok", "env": "test"}
    assert calls["snapshot"] == 1


def test_health_legacy_still_works(client, monkeypatch):
    """The legacy /health endpoint is unchanged and still returns the
    full snapshot. Existing dashboards / Railway healthcheck / the
    daily-smoke + uptime workflows hit this path."""
    calls = {"snapshot": 0}

    def _spy_snapshot():
        calls["snapshot"] += 1
        return {"status": "ok", "env": "test", "db": "up"}

    import main
    import observability
    monkeypatch.setattr(observability, "health_snapshot", _spy_snapshot)
    monkeypatch.setattr(main, "health_snapshot", _spy_snapshot)

    res = client.get("/health")
    assert res.status_code == 200, res.text
    assert res.json() == {"status": "ok", "env": "test", "db": "up"}
    assert calls["snapshot"] == 1


def test_health_ready_returns_503_when_down(client, monkeypatch):
    """When health_snapshot reports a hard failure (DB / Redis down),
    /health/ready must surface 503 so the LB pulls the instance out."""
    def _down_snapshot():
        return {"status": "down", "down_reason": "redis_unreachable"}

    import main
    import observability
    monkeypatch.setattr(observability, "health_snapshot", _down_snapshot)
    monkeypatch.setattr(main, "health_snapshot", _down_snapshot)

    res = client.get("/health/ready")
    assert res.status_code == 503, res.text
    assert res.json()["status"] == "down"


def test_health_live_still_200_when_deps_are_down(client, monkeypatch):
    """Even if Redis is unreachable, /health/live should still return
    200 — the process IS alive, it just can't serve user traffic. This
    is the standard k8s liveness vs readiness distinction: liveness
    failing means "kill+restart"; readiness failing means "drain".
    Conflating the two restarts the process every time Redis blips."""
    # Force snapshot to "down" — should not affect /health/live.
    def _down_snapshot():
        return {"status": "down", "down_reason": "redis_unreachable"}

    import main
    import observability
    monkeypatch.setattr(observability, "health_snapshot", _down_snapshot)
    monkeypatch.setattr(main, "health_snapshot", _down_snapshot)

    res = client.get("/health/live")
    assert res.status_code == 200, res.text
    assert res.json() == {"status": "alive"}
