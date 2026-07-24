"""Reaper heartbeat tests.

The reaper runs as a daemon thread (main.py:_reaper_loop). If it dies
silently (raised + uncaught, deadlock, OOM kill, etc.) jobs stuck in
processing/queued/transcribed_pending pile up forever and nobody is
alerted until a user complains.

The heartbeat fix:
  - After every successful sweep, the loop calls mark_reaper_ok() which
    bumps a module-level monotonic timestamp.
  - health_snapshot() reads that timestamp; if more than
    REAPER_STALLED_AFTER_S have elapsed since the last tick, status is
    flagged "degraded" with reason "reaper_stalled_<N>s".
  - For the first 5 min of process lifetime (cold start) the heartbeat
    is allowed to be NULL (the reaper sleeps 60s before its first
    sweep). After that, NULL means the thread never started → degraded.

These tests pin the contract:
  • mark_reaper_ok() updates the timestamp idempotently
  • reaper_seconds_since_last_ok() returns None pre-tick, float post-tick
  • health_snapshot() degrades when stalled
  • health_snapshot() doesn't false-positive during cold start
"""
from __future__ import annotations

import time
import importlib


def _reload_observability():
    """Fresh module state per test. Sets process start to NOW so
    cold-start grace is fresh."""
    import observability as obs
    obs._PROCESS_START_TS = time.monotonic()
    obs._REAPER_LAST_OK_TS = None
    obs.REAPER_STALLED_AFTER_S = 700  # default
    obs.STARTUP_GRACE_S = 20
    return obs


# ─── mark_reaper_ok contract ──────────────────────────────────────────

def test_initial_state_is_none():
    obs = _reload_observability()
    assert obs.reaper_seconds_since_last_ok() is None, (
        "fresh process must report None until the first tick"
    )


def test_mark_reaper_ok_updates_timestamp():
    obs = _reload_observability()
    obs.mark_reaper_ok()
    secs = obs.reaper_seconds_since_last_ok()
    assert secs is not None and secs < 0.5, (
        f"first tick should be near-zero seconds ago, got {secs}"
    )


def test_mark_reaper_ok_idempotent_keeps_latest():
    """Multiple ticks in quick succession — each one resets the timer.
    The latest tick is what counts, not the first."""
    obs = _reload_observability()
    obs.mark_reaper_ok()
    time.sleep(0.1)
    obs.mark_reaper_ok()  # latest
    secs = obs.reaper_seconds_since_last_ok()
    assert secs is not None and secs < 0.1, (
        f"latest tick should reset the clock, got {secs}"
    )


# ─── health_snapshot integration ──────────────────────────────────────

def test_health_cold_start_no_tick_does_not_degrade():
    """During the first 5 min after process start, NO tick is OK — the
    reaper thread sleeps 60s before its first sweep, then takes up to
    a few minutes to land its first OK depending on DB latency. We
    can't degrade /health during this window or we'd false-positive
    every deploy."""
    obs = _reload_observability()
    obs._REAPER_LAST_OK_TS = None
    # Process just started → cold start window
    snap = obs.health_snapshot()
    # Reaper must be reported as cold_start, NOT degrade the snap
    assert snap.get("reaper") == "cold_start"
    # The snap might be "starting" due to other deps during grace, but
    # it shouldn't be "degraded" because of the reaper specifically.
    if snap.get("status") == "degraded":
        assert "reaper" not in snap.get("degraded_reason", ""), (
            f"reaper shouldn't degrade during cold start, got "
            f"{snap.get('degraded_reason')}"
        )


def test_health_never_ticked_after_grace_degrades():
    """If the cold-start grace expires and the reaper still never
    ticked, the thread is dead/missing. Degrade with a clear reason."""
    obs = _reload_observability()
    obs._REAPER_LAST_OK_TS = None
    # Simulate 6 min of process uptime — past the 5 min cold-start grace
    obs._PROCESS_START_TS = time.monotonic() - 360
    snap = obs.health_snapshot()
    assert snap.get("reaper") == "never_ticked"
    assert snap.get("status") == "degraded"
    assert snap.get("degraded_reason") == "reaper_never_ticked"


def test_health_recent_tick_status_ok():
    obs = _reload_observability()
    obs.mark_reaper_ok()
    snap = obs.health_snapshot()
    reaper = snap.get("reaper")
    assert isinstance(reaper, dict)
    assert reaper["seconds_since_last_ok"] < 1
    assert "reaper" not in (snap.get("degraded_reason") or "")


def test_health_stalled_tick_degrades():
    """If the last tick was > REAPER_STALLED_AFTER_S ago, the reaper
    is silently stuck — degrade."""
    obs = _reload_observability()
    # Simulate an old tick: monotonic value from 800 seconds ago
    obs._REAPER_LAST_OK_TS = time.monotonic() - 800
    snap = obs.health_snapshot()
    reaper = snap.get("reaper")
    assert isinstance(reaper, dict)
    assert reaper["seconds_since_last_ok"] >= 800
    assert snap.get("status") == "degraded"
    reason = snap.get("degraded_reason") or ""
    assert reason.startswith("reaper_stalled_")


def test_health_stalled_threshold_configurable():
    """Operators can loosen the threshold via env var without
    redeploy — useful in staging where traffic is bursty."""
    obs = _reload_observability()
    obs.REAPER_STALLED_AFTER_S = 1200  # 20 min — looser
    # Tick was 800s ago — within new threshold
    obs._REAPER_LAST_OK_TS = time.monotonic() - 800
    snap = obs.health_snapshot()
    # Reaper field present and not stalled
    assert isinstance(snap.get("reaper"), dict)
    reason = snap.get("degraded_reason") or ""
    assert not reason.startswith("reaper_stalled_"), (
        f"800s should be within 1200s threshold, got {reason}"
    )
