"""Tests for subprocess_utils.run_checked and the SubprocessExecutionError shape.

These mock ``subprocess.run`` directly — no real ffmpeg is spawned, which
matches the existing pattern in ``test_content_validator.py``. The goal
is to lock the public contract of ``run_checked``:

  * raises on rc!=0, on timeout, and on rc=0 with a missing/empty
    output file
  * surfaces label, returncode, stderr_tail, timed_out, output_problem,
    and duration_s on the exception
  * is a subclass of RuntimeError so existing
    ``except RuntimeError`` handlers in pipeline.py keep working
  * deletes the partial output when cleanup_on_failure is True
"""

import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

import subprocess_utils
from subprocess_utils import (
    SubprocessExecutionError,
    close_popen_streams,
    run_checked,
)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_run_checked_returns_completed_process_on_success(tmp_path):
    """rc=0 with a non-empty output_path → returns the CompletedProcess
    unchanged so callers that need stdout/stderr can still read them."""
    out = tmp_path / "ok.mp4"
    out.write_bytes(b"\x00" * 1024)  # 1 KB non-empty

    with patch.object(subprocess_utils.subprocess, "run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["ffmpeg", "-i", "in", str(out)],
            returncode=0,
            stdout="",
            stderr="frame= 100 fps=30",
        )
        result = run_checked(
            ["ffmpeg", "-i", "in", str(out)],
            label="ffmpeg-ok",
            timeout=10,
            output_path=str(out),
        )

    assert isinstance(result, subprocess.CompletedProcess)
    assert result.returncode == 0
    assert result.stderr == "frame= 100 fps=30"


def test_run_checked_without_output_path_skips_existence_check(tmp_path):
    """A caller that doesn't care about a specific output file (e.g.
    ffprobe printing to stdout) should not be forced to pass a path."""
    with patch.object(subprocess_utils.subprocess, "run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["ffprobe", "x"],
            returncode=0,
            stdout='{"duration": "12.5"}',
            stderr="",
        )
        result = run_checked(
            ["ffprobe", "x"], label="ffprobe-probe", timeout=10,
        )

    assert result.returncode == 0
    assert "duration" in result.stdout


# ---------------------------------------------------------------------------
# Non-zero returncode
# ---------------------------------------------------------------------------

def test_run_checked_raises_on_nonzero_returncode(tmp_path):
    """rc=1 → SubprocessExecutionError carrying the rc and a stderr tail."""
    out = tmp_path / "should_be_gone.mp4"

    with patch.object(subprocess_utils.subprocess, "run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["ffmpeg", "-i", "in", str(out)],
            returncode=1,
            stdout="",
            stderr="Invalid argument near 'foo'",
        )
        with pytest.raises(SubprocessExecutionError) as excinfo:
            run_checked(
                ["ffmpeg", "-i", "in", str(out)],
                label="ffmpeg-broken",
                timeout=10,
                output_path=str(out),
            )

    err = excinfo.value
    assert err.label == "ffmpeg-broken"
    assert err.returncode == 1
    assert "Invalid argument near 'foo'" in err.stderr_tail
    assert err.timed_out is False
    assert err.output_problem is None
    assert err.duration_s is not None and err.duration_s >= 0
    # Message is informative — contains the label, rc, and stderr tail.
    msg = str(err)
    assert "ffmpeg-broken" in msg
    assert "rc=1" in msg
    assert "Invalid argument" in msg


def test_run_checked_stderr_tail_is_truncated_to_500_chars():
    """A 10 KB stderr (real ffmpeg crash repro) must be tailed to keep
    log lines and Job.error column manageable."""
    huge = "X" * 10_000
    with patch.object(subprocess_utils.subprocess, "run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["ffmpeg"], returncode=1, stdout="", stderr=huge,
        )
        with pytest.raises(SubprocessExecutionError) as excinfo:
            run_checked(["ffmpeg"], label="ffmpeg-huge", timeout=10)

    assert len(excinfo.value.stderr_tail) == 500
    assert excinfo.value.stderr_tail == "X" * 500


# ---------------------------------------------------------------------------
# rc=0 but output missing / empty (the silent-corruption catch)
# ---------------------------------------------------------------------------

def test_run_checked_raises_when_output_missing_despite_rc_zero(tmp_path):
    """ffmpeg occasionally exits clean while producing no file at all
    (filter graph misconfig). The helper catches this rather than
    letting the downstream code blow up on a stat() later."""
    out = tmp_path / "vanished.mp4"  # never created

    with patch.object(subprocess_utils.subprocess, "run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["ffmpeg"], returncode=0, stdout="", stderr="",
        )
        with pytest.raises(SubprocessExecutionError) as excinfo:
            run_checked(
                ["ffmpeg"],
                label="ffmpeg-noop",
                timeout=10,
                output_path=str(out),
            )

    assert excinfo.value.returncode == 0
    assert excinfo.value.output_problem == "missing"
    assert excinfo.value.output_path == str(out)


def test_run_checked_raises_when_output_zero_bytes_despite_rc_zero(tmp_path):
    """The disk-full repro: ffmpeg opens the output, the disk fills,
    ffmpeg flushes 0 bytes and exits 0. We catch the 0-byte file."""
    out = tmp_path / "empty.mp4"
    out.touch()  # 0-byte file

    with patch.object(subprocess_utils.subprocess, "run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["ffmpeg"], returncode=0, stdout="", stderr="",
        )
        with pytest.raises(SubprocessExecutionError) as excinfo:
            run_checked(
                ["ffmpeg"],
                label="ffmpeg-disk-full",
                timeout=10,
                output_path=str(out),
            )

    assert excinfo.value.output_problem == "empty"


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------

def test_run_checked_raises_on_timeout(tmp_path):
    """TimeoutExpired → wrapped in SubprocessExecutionError with
    timed_out=True and returncode=None so callers can distinguish
    "ffmpeg crashed" from "ffmpeg hung past its budget"."""
    out = tmp_path / "interrupted.mp4"

    with patch.object(subprocess_utils.subprocess, "run") as mock_run:
        mock_run.side_effect = subprocess.TimeoutExpired(
            cmd=["ffmpeg"], timeout=0.001, output=b"", stderr=b"hung at frame 42",
        )
        with pytest.raises(SubprocessExecutionError) as excinfo:
            run_checked(
                ["ffmpeg"],
                label="ffmpeg-hung",
                timeout=0.001,
                output_path=str(out),
            )

    err = excinfo.value
    assert err.timed_out is True
    assert err.returncode is None
    assert "hung at frame 42" in err.stderr_tail
    assert "timed out" in str(err)


# ---------------------------------------------------------------------------
# Cleanup on failure
# ---------------------------------------------------------------------------

def test_run_checked_cleans_up_partial_output_on_failure(tmp_path):
    """Default cleanup_on_failure=True must unlink a partially-written
    output file so the next attempt doesn't see stale bytes."""
    out = tmp_path / "partial.mp4"
    out.write_bytes(b"half-written ffmpeg output")
    assert out.exists()

    with patch.object(subprocess_utils.subprocess, "run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["ffmpeg"], returncode=1, stdout="", stderr="oops",
        )
        with pytest.raises(SubprocessExecutionError):
            run_checked(
                ["ffmpeg"],
                label="ffmpeg-cleanup",
                timeout=10,
                output_path=str(out),
            )

    assert not out.exists(), "partial output should have been unlinked"


def test_run_checked_skips_cleanup_when_flag_is_false(tmp_path):
    """Callers managing their own cleanup (libass renders preserve the
    partial for debug) can opt out."""
    out = tmp_path / "kept.mp4"
    out.write_bytes(b"keep me for debug")

    with patch.object(subprocess_utils.subprocess, "run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["ffmpeg"], returncode=1, stdout="", stderr="oops",
        )
        with pytest.raises(SubprocessExecutionError):
            run_checked(
                ["ffmpeg"],
                label="ffmpeg-keep",
                timeout=10,
                output_path=str(out),
                cleanup_on_failure=False,
            )

    assert out.exists(), "partial output should have been kept"


# ---------------------------------------------------------------------------
# API contracts
# ---------------------------------------------------------------------------

def test_subprocess_execution_error_is_runtime_error_subclass():
    """Backward compat: existing `except RuntimeError:` blocks in
    pipeline.py must continue to catch failures from run_checked."""
    err = SubprocessExecutionError("x", returncode=1)
    assert isinstance(err, RuntimeError)


def test_run_checked_rejects_check_kwarg():
    """Silently honoring check= would let subprocess.run raise its own
    CalledProcessError, shadowing our richer error type. Fail loudly."""
    with pytest.raises(TypeError, match="check="):
        run_checked(["true"], label="x", timeout=1, check=True)


# ---------------------------------------------------------------------------
# close_popen_streams
# ---------------------------------------------------------------------------

def test_close_popen_streams_closes_both_streams():
    """Drive uploader path: explicit close before proc.wait()."""
    fake = MagicMock(spec=subprocess.Popen)
    fake.stdout = MagicMock()
    fake.stderr = MagicMock()

    close_popen_streams(fake)

    fake.stdout.close.assert_called_once()
    fake.stderr.close.assert_called_once()


def test_close_popen_streams_tolerates_none_streams():
    """Popen without PIPE redirects has stdout/stderr=None — must not raise."""
    fake = MagicMock(spec=subprocess.Popen)
    fake.stdout = None
    fake.stderr = None

    close_popen_streams(fake)  # no assertion; just must not raise


def test_close_popen_streams_swallows_close_errors():
    """Best-effort: a stream that raises on close() must not prevent the
    other stream from being closed, and must not propagate to the
    caller's error path."""
    fake = MagicMock(spec=subprocess.Popen)
    fake.stdout = MagicMock()
    fake.stdout.close.side_effect = ValueError("already closed")
    fake.stderr = MagicMock()  # still gets closed

    close_popen_streams(fake)  # must not raise

    fake.stderr.close.assert_called_once()
