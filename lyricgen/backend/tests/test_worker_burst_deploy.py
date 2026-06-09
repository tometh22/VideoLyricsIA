"""Regression tests for WarmOnlyWorker — burst-deploy protection.

Background (incident 2026-06-03, agus.cafisi prod job loss on a high-deploy day):
stock RQ escalates a SECOND shutdown signal to a COLD shutdown — it kills the
work-horse and raises SystemExit. When two deploys land back-to-back, the 2nd
deploy's SIGTERM hits the worker while it is STILL draining the 1st deploy's
render, RQ cold-kills it, and the in-flight render is lost.

worker.WarmOnlyWorker overrides request_force_stop so every shutdown signal
stays a WARM shutdown (the render runs to completion). The hard caps remain
(job timeout + Railway's RAILWAY_SHUTDOWN_TIMEOUT_SECONDS SIGKILL), so a truly
hung worker still dies — we just never throw away a render that's progressing.

These tests are CI-safe (no Redis, no fakeredis, no subprocess). They guard the
two things that can silently break the fix:
  1. RQ renaming/removing `request_force_stop` in an upgrade → our override
     would become dead code and burst deploys would cold-kill again.
  2. Someone "simplifying" request_force_stop back to escalating.

The end-to-end behavioural proof (double-SIGTERM over fakeredis lets the job
COMPLETE) was run manually against this exact subclass on 2026-06-03; it relies
on multiprocessing+signals and is too flaky to live in CI.
"""

import datetime as _dt
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from rq import Worker as _RQWorker

import worker
from worker import WarmOnlyWorker, _should_recycle


def _stub_worker(stop_requested=False, shutdown_date=None):
    # SimpleNamespace (not MagicMock): a MagicMock auto-creates
    # _shutdown_requested_date as a truthy child mock, which would break the
    # `is not None` check in _should_recycle. We set exactly the two attrs.
    return SimpleNamespace(
        _stop_requested=stop_requested,
        _shutdown_requested_date=shutdown_date,
    )


def test_is_rq_worker_subclass():
    # If this fails, worker.work() would no longer use RQ's machinery at all.
    assert issubclass(WarmOnlyWorker, _RQWorker)


def test_override_is_live_against_rq_api():
    # The fix only works if RQ still HAS request_force_stop (so our override
    # actually replaces the cold-kill path) AND our version differs from it.
    # A future RQ that renames this method would trip this test instead of
    # silently regressing to cold-kill.
    assert hasattr(_RQWorker, "request_force_stop"), (
        "RQ no longer exposes request_force_stop — the burst-deploy override "
        "is now dead code; re-point WarmOnlyWorker at the new cold-shutdown hook."
    )
    assert WarmOnlyWorker.request_force_stop is not _RQWorker.request_force_stop


def test_request_force_stop_does_not_escalate():
    # The whole point: a 2nd shutdown signal must NOT cold-kill. Call the
    # override on a stub worker and assert it neither raises (SystemExit is how
    # stock RQ aborts) nor touches the cold-shutdown machinery.
    stub = MagicMock(spec=WarmOnlyWorker)
    # Unbound call so we control `self` and can inspect what it touches.
    WarmOnlyWorker.request_force_stop(stub, 15, None)  # SIGTERM, no frame
    stub.kill_horse.assert_not_called()
    stub.stop_scheduler.assert_not_called()
    # No SystemExit / RuntimeError escaped — reaching here is the assertion.


# --- Self-recycle discriminator (P0 2026-06-08, hardened 2026-06-09) ---------
# main() re-execs a fresh process when work() returns for a recyclable reason,
# and exits (for the rolling deploy) on a warm shutdown.

def test_recycle_on_max_jobs_cap():
    # max_jobs recycle (and Redis-blip breaks) leave BOTH shutdown flags unset
    # → we MUST respawn so the queue keeps a consumer. This is the bug the P0
    # fixed: a clean exit here was never restarted under ON_FAILURE.
    assert _should_recycle(_stub_worker()) is True


def test_no_recycle_on_busy_warm_shutdown():
    # SIGTERM while BUSY sets _stop_requested True → exit (not re-exec).
    assert _should_recycle(_stub_worker(stop_requested=True)) is False


def test_no_recycle_on_idle_warm_shutdown():
    # REGRESSION (adversarial review 2026-06-09): RQ's _shutdown sets
    # _stop_requested ONLY when BUSY; an IDLE worker (the ShortWorker's normal
    # state) gets SIGTERM, raises StopRequested, and leaves _stop_requested
    # False while request_stop DID set _shutdown_requested_date. Keying only on
    # _stop_requested misfired here → os.execve fighting the deploy. Must NOT
    # recycle when _shutdown_requested_date is set.
    w = _stub_worker(stop_requested=False, shutdown_date=_dt.datetime(2026, 6, 9))
    assert _should_recycle(w) is False


def test_recycle_discriminator_is_live():
    # _should_recycle reads `_stop_requested` AND `_shutdown_requested_date`;
    # if a future RQ renames either, the discriminator silently regresses
    # (always-recycle, re-exec'ing through deploys). Pin both attributes.
    init_names = _RQWorker.__init__.__code__.co_names
    assert hasattr(_RQWorker, "_stop_requested") or "_stop_requested" in init_names, (
        "RQ no longer sets _stop_requested — re-point _should_recycle()."
    )
    assert "_shutdown_requested_date" in init_names, (
        "RQ no longer sets _shutdown_requested_date — re-point _should_recycle()."
    )


# --- os.execve recycle path + no-work circuit breaker (2026-06-09) -----------

def test_recycle_or_exit_execs_on_max_jobs(monkeypatch):
    # did_work=True + no shutdown flags ⇒ re-exec a fresh process.
    captured = {}
    monkeypatch.setattr(worker.os, "execve", lambda *a: captured.setdefault("argv", a[1]))
    monkeypatch.setattr(worker.time, "sleep", lambda _s: None)
    worker._recycle_or_exit(_stub_worker(), did_work=True, max_jobs=10)
    assert captured.get("argv", [None])[0] == worker.sys.executable


def test_recycle_or_exit_exits_on_warm_shutdown(monkeypatch):
    # warm shutdown ⇒ return (caller exits), NEVER execve.
    monkeypatch.setattr(worker.os, "execve",
                        lambda *a: pytest.fail("must not re-exec on warm shutdown"))
    assert worker._recycle_or_exit(
        _stub_worker(stop_requested=True), did_work=True, max_jobs=10) is None


def test_recycle_or_exit_circuit_breaker_exits_after_noprogress(monkeypatch):
    # A barren no-work streak reaching the threshold ⇒ sys.exit(1) so Railway's
    # bounded ON_FAILURE takes over (execve would hot-loop with the same PID).
    monkeypatch.setattr(worker.os, "execve",
                        lambda *a: pytest.fail("must exit(1), not hot-loop execve"))
    monkeypatch.setattr(worker.time, "sleep", lambda _s: None)
    monkeypatch.setenv(worker._NOWORK_STREAK_ENV, str(worker._MAX_NOWORK_RECYCLES - 1))
    with pytest.raises(SystemExit) as ei:
        worker._recycle_or_exit(_stub_worker(), did_work=False, max_jobs=10)
    assert ei.value.code == 1
