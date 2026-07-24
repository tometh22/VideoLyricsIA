"""Tier 4 — render resource fixes.

Guards the two highest-value, unit-testable fixes:
  - C5  _cleanup_job_dir_on_failure: a failed render frees its disk (the
        multi-GB-intermediates-pile-up-until-disk-full cascade).
  - H3  _call_with_timeout: a timeout returns CONTROL immediately instead of
        re-blocking on the orphaned thread via the executor's shutdown(wait=True)
        (the bug the adversarial review flagged: the wrapper claimed to unblock
        the worker but didn't).

(H2 ffprobe timeout and H6 ffmpeg -threads are exercised only against a real
binary, so they're covered by CI's render gate + manual staging soak, not here.)
"""

import time

import pytest

import pipeline


# --- C5: failed-render disk cleanup ---
def test_cleanup_job_dir_on_failure_removes_everything(tmp_path):
    d = tmp_path / "job123"
    d.mkdir()
    (d / "bg_generated.mp4").write_bytes(b"x" * 1024)
    (d / "umg_master.mov").write_bytes(b"x" * 1024)
    (d / "input.mp3").write_bytes(b"x")
    pipeline._cleanup_job_dir_on_failure(str(d))
    assert not d.exists()  # whole dir gone — retry re-downloads from R2


def test_cleanup_job_dir_kill_switch_keeps_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEANUP_FAILED_JOB_DIR", "false")
    d = tmp_path / "job"
    d.mkdir()
    (d / "f.mp4").write_bytes(b"x")
    pipeline._cleanup_job_dir_on_failure(str(d))
    assert d.exists()  # kill-switch preserves it for incident triage


def test_cleanup_job_dir_missing_is_safe():
    # No raise on a missing/blank path (best-effort contract).
    pipeline._cleanup_job_dir_on_failure("/nonexistent/path/abc123")
    pipeline._cleanup_job_dir_on_failure("")
    pipeline._cleanup_job_dir_on_failure(None)


# --- H3: timeout must NOT re-block on the orphan ---
def test_call_with_timeout_returns_value_fast():
    assert pipeline._call_with_timeout(lambda: 42, 5.0, label="ok") == 42


def test_call_with_timeout_does_not_reblock_on_orphan():
    """THE regression guard for the executor-shutdown(wait=True) bug: a fn that
    sleeps far longer than the timeout must make _call_with_timeout raise
    PROMPTLY (~timeout), NOT wait ~3s for the orphan thread to finish."""
    def slow():
        time.sleep(3.0)
    t0 = time.time()
    with pytest.raises(TimeoutError):
        pipeline._call_with_timeout(slow, 0.4, label="slow")
    elapsed = time.time() - t0
    assert elapsed < 1.5, f"re-blocked on the orphan thread: took {elapsed:.1f}s (expected ~0.4s)"


def test_call_with_timeout_propagates_fn_error():
    def boom():
        raise ValueError("nope")
    with pytest.raises(ValueError):
        pipeline._call_with_timeout(boom, 5.0)
