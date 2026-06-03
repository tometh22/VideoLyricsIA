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

from unittest.mock import MagicMock

from rq import Worker as _RQWorker

from worker import WarmOnlyWorker


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
