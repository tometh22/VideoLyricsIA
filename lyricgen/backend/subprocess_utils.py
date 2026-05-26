"""Subprocess utilities for the render pipeline.

Standardizes ``subprocess.run()`` calls that produce deliverable files
(ProRes masters, libass renders, BG fallbacks, Drive uploads) so the
worker log, Sentry breadcrumb, and ``Job.error`` column tell the same
story when something fails.

Scope: critical-path subprocess only. Probing / telemetry / best-effort
fallback subprocess keep their inline ``if rc != 0`` patterns — those
have legitimate fail-open semantics and don't need a uniform error
shape.

Design notes:
  * :class:`SubprocessExecutionError` is a subclass of ``RuntimeError``
    so all existing ``except RuntimeError:`` blocks in the pipeline
    keep working unchanged. New callers can ``except
    SubprocessExecutionError`` to read ``.label`` / ``.returncode`` /
    ``.stderr_tail`` / ``.timed_out`` / ``.output_problem`` /
    ``.duration_s`` for richer context in Sentry or job.error.
  * :func:`run_checked` mandates an explicit ``timeout``. There is no
    sensible default — a 1-second ffprobe and a 10-minute ProRes
    transcode live in different orders of magnitude.
  * ``output_path`` is the silent-corruption catch: ffmpeg has been
    observed to return rc=0 with a 0-byte output when the disk
    fills mid-write, or when a filter graph silently elides frames.
    The downstream pipeline blindly trusts the path; this helper
    doesn't.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from typing import Any

logger = logging.getLogger("subprocess_utils")

# stderr/stdout payloads can be MB-large on ffmpeg crashes. We surface
# the tail because that's where the actual error message lives — the
# stack of repeated decoder warnings before it is noise. Matches the
# existing inline pattern in pipeline.py:7158 (``result.stderr[-500:]``).
_STDERR_TAIL_CHARS = 500


class SubprocessExecutionError(RuntimeError):
    """A subprocess we expected to produce a deliverable file failed.

    Attributes
    ----------
    label : str
        Short identifier set by the caller (e.g. ``"ffmpeg-prores"``).
        Goes into the log line and the str(exc) so the worker log can
        tell apart the half-dozen ffmpeg calls in the render path.
    returncode : int | None
        Process exit code, or ``None`` when the subprocess was killed
        by timeout before it could exit.
    stderr_tail : str
        Last ``_STDERR_TAIL_CHARS`` characters of stderr (or the merged
        stderr+stdout for Popen callers that pre-truncate).
    timed_out : bool
        ``True`` iff the failure was a :class:`subprocess.TimeoutExpired`.
    output_path : str | None
        Path the caller expected to be created. Set on output-verification
        failures and on cleanup attempts.
    output_problem : str | None
        ``"missing"``, ``"empty"``, or ``"unstatable (<errno>)"`` when the
        process exited rc=0 but the output didn't materialize.
    duration_s : float | None
        Wall-clock seconds spent in the subprocess. Useful for
        distinguishing "ffmpeg crashed at startup" from "ffmpeg ran for
        9 minutes then crashed".
    """

    def __init__(
        self,
        label: str,
        returncode: int | None,
        *,
        stderr_tail: str = "",
        timed_out: bool = False,
        output_path: str | None = None,
        output_problem: str | None = None,
        duration_s: float | None = None,
    ):
        self.label = label
        self.returncode = returncode
        self.stderr_tail = stderr_tail
        self.timed_out = timed_out
        self.output_path = output_path
        self.output_problem = output_problem
        self.duration_s = duration_s

        if timed_out:
            head = f"{label} timed out"
        elif output_problem:
            head = f"{label} produced {output_problem} output"
        else:
            head = f"{label} failed (rc={returncode})"

        details = []
        if duration_s is not None:
            details.append(f"after {duration_s:.1f}s")
        if output_path:
            details.append(f"output={os.path.basename(output_path)}")
        if stderr_tail:
            details.append(f"stderr: {stderr_tail}")
        super().__init__(head + (" — " + " — ".join(details) if details else ""))


def _tail(text: str | bytes | None, n: int = _STDERR_TAIL_CHARS) -> str:
    """Return the last ``n`` characters of ``text``, stripped.

    Accepts None / bytes / str. Bytes are decoded utf-8 with replacement
    so a garbled ffmpeg stderr never becomes the reason the error
    message itself crashes.
    """
    if not text:
        return ""
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover — extremely defensive
            text = repr(text)
    s = text.strip()
    return s[-n:] if len(s) > n else s


def _cleanup_partial(path: str | None) -> None:
    """Best-effort unlink for a partially-written output file.

    Mirrors the existing pattern at pipeline.py:7150-7155. Logs (does
    not raise) on failure — a leftover partial is annoying but not
    fatal; raising here would mask the real subprocess failure.
    """
    if not path:
        return
    try:
        if os.path.exists(path):
            os.unlink(path)
    except OSError as e:
        logger.warning(
            "[subprocess_utils] cleanup_partial failed for %s: %s", path, e,
        )


def run_checked(
    cmd: list[str],
    *,
    label: str,
    timeout: float,
    output_path: str | None = None,
    cleanup_on_failure: bool = True,
    capture_output: bool = True,
    text: bool = True,
    **kwargs: Any,
) -> subprocess.CompletedProcess:
    """Run ``subprocess.run(cmd, ...)`` and raise on failure.

    Failures detected (in order):
      1. :class:`subprocess.TimeoutExpired` — raised as
         :class:`SubprocessExecutionError` with ``timed_out=True``,
         ``returncode=None``.
      2. Non-zero ``returncode`` — raised with the rc and a stderr tail.
      3. ``output_path`` provided but missing or zero-byte after rc=0 —
         raised with ``output_problem="missing"`` / ``"empty"``.

    On any of the three failures, when ``cleanup_on_failure=True``
    (default), the partial ``output_path`` is unlinked best-effort.

    Parameters
    ----------
    cmd : list[str]
        Command argv. ``shell=True`` is intentionally not supported —
        all current callers pass an argv and shell-quoted strings are
        the most common way subprocess code grows footguns.
    label : str
        Human-readable identifier for logs ("ffmpeg-prores",
        "rclone-drive"). Appears in every line this helper emits.
    timeout : float
        Hard limit in seconds. **Required** — there is no sane default.
    output_path : str | None
        If set, the file at this path must exist and be non-empty after
        the subprocess succeeds; otherwise raises.
    cleanup_on_failure : bool, default True
        Unlink ``output_path`` before raising on any failure.
    capture_output, text : bool
        Forwarded to ``subprocess.run``. Defaults match the common
        ffmpeg/ffprobe case where we want stderr for the error message.
    **kwargs
        Forwarded to ``subprocess.run``. Passing ``check=`` raises
        ``TypeError`` — this helper does its own checking and the two
        modes don't compose cleanly.

    Returns
    -------
    subprocess.CompletedProcess
        Only on full success. Callers that need ``result.stdout`` (e.g.
        ffprobe JSON output) can still read it normally.
    """
    if "check" in kwargs:
        raise TypeError(
            "run_checked() does its own checking; do not pass check=. "
            "Use subprocess.run() directly if you want CalledProcessError."
        )

    logger.info(
        "[subprocess_utils] %s starting (timeout=%ss): %s",
        label, timeout,
        " ".join(cmd[:6]) + (" …" if len(cmd) > 6 else ""),
    )
    started = time.monotonic()

    try:
        result = subprocess.run(
            cmd,
            timeout=timeout,
            capture_output=capture_output,
            text=text,
            **kwargs,
        )
    except subprocess.TimeoutExpired as e:
        duration = time.monotonic() - started
        stderr_tail = _tail(getattr(e, "stderr", None))
        logger.error(
            "[subprocess_utils] %s TIMED OUT after %.1fs (limit=%ss); stderr tail: %s",
            label, duration, timeout, stderr_tail or "<none>",
        )
        if cleanup_on_failure:
            _cleanup_partial(output_path)
        raise SubprocessExecutionError(
            label,
            returncode=None,
            stderr_tail=stderr_tail,
            timed_out=True,
            output_path=output_path,
            duration_s=duration,
        ) from e

    duration = time.monotonic() - started

    if result.returncode != 0:
        stderr_tail = _tail(result.stderr)
        logger.error(
            "[subprocess_utils] %s FAILED rc=%s after %.1fs; stderr tail: %s",
            label, result.returncode, duration, stderr_tail or "<none>",
        )
        if cleanup_on_failure:
            _cleanup_partial(output_path)
        raise SubprocessExecutionError(
            label,
            returncode=result.returncode,
            stderr_tail=stderr_tail,
            output_path=output_path,
            duration_s=duration,
        )

    # rc=0 path — verify output if requested. A returncode of 0 from
    # ffmpeg has been observed alongside a 0-byte output when the disk
    # fills mid-write, or when a filter graph silently elides frames.
    if output_path is not None:
        problem: str | None = None
        if not os.path.exists(output_path):
            problem = "missing"
        else:
            try:
                if os.path.getsize(output_path) == 0:
                    problem = "empty"
            except OSError as e:
                problem = f"unstatable ({e})"
        if problem:
            stderr_tail = _tail(result.stderr)
            logger.error(
                "[subprocess_utils] %s rc=0 but output %s (%s) after %.1fs; stderr tail: %s",
                label, problem, output_path, duration, stderr_tail or "<none>",
            )
            if cleanup_on_failure:
                _cleanup_partial(output_path)
            raise SubprocessExecutionError(
                label,
                returncode=0,
                stderr_tail=stderr_tail,
                output_path=output_path,
                output_problem=problem,
                duration_s=duration,
            )

    logger.info("[subprocess_utils] %s OK in %.1fs", label, duration)
    return result


def close_popen_streams(proc: subprocess.Popen) -> None:
    """Close stdout/stderr explicitly before ``proc.wait()``.

    Cheap insurance: when a Popen reader exits its read loop, Python's
    GC eventually closes the streams, but timing is non-deterministic.
    Explicit close before ``wait()`` avoids the rare case where the
    child blocks on a write to a buffer Python hasn't drained yet —
    relevant for long-running uploads (rclone) where the child can
    outlive the parent's loop iteration.

    Safe to call with already-closed streams; ignores failures.
    """
    for stream in (proc.stdout, proc.stderr):
        if stream is None:
            continue
        try:
            stream.close()
        except Exception:  # pragma: no cover — best effort
            pass
