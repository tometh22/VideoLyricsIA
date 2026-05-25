"""Bounded retry helper for Replicate calls — shared across forced_align,
whisperX, vocal_sep.

WHY THIS EXISTS
---------------
QA full run 2026-05-24 revealed that an unbounded retry loop around
`replicate.run(...)` burns hours of wall-clock during Replicate
degraded windows. Cosas Mías: 12048 s elapsed (3h20min). Intoxicados:
16057 s (4h27min). Three attempts × ~90 min HTTP-timeout each.

PR #281 added a budget cap inside `forced_align.py`. The audit caught
that whisperX and vocal_sep have the SAME unbounded retry pattern and
would repeat the incident on a different code path. Extracting to a
shared module:

1. closes that latent bug (whisperX + vocal_sep get the same protection),
2. centralises the non-retryable error fragment list so all three
   call sites stay in sync,
3. lets us add tags / Sentry breadcrumbs / metrics in one place.

CONTRACT
--------
- `call_with_budget(model, input_factory, *, total_budget_s, backoff,
   call_label="replicate") -> Optional[Any]`
- `call_label` is a short tag for logs (`forced`, `whisperx`, `demucs`).
- Returns the model output on success, or None on abort.
- Never raises (callers expect None on every failure mode).
"""
from __future__ import annotations

import logging
import os
import time as _t
from typing import Callable, Optional

logger = logging.getLogger("genly.replicate_budget")


# Error message fragments (lowercase substring match) that won't get
# better on retry. Aborting on these saves 32 s of sleeps + 3 RTTs.
NON_RETRYABLE_FRAGMENTS = (
    "padding size",          # cureau `[1, 2, 1]` tensor crash (PR #281)
    "argument #4",
    "validationerror",
    "validation error",
    "invalid input",
    "unsupported audio",
    "transcript too long",
    "model is not currently available",  # Replicate-specific terminal
    "authentication required",
    "not authorized",
    # HOTFIX 2026-05-24 (operator: "ETA stuck en 122s nunca avanzó"):
    # cureau also crashes with `[1, 2, 0]` (zero-sample tensor —
    # happens when the audio file has metadata duration > 0 but the
    # actual sample payload is empty/silent). ffprobe-based
    # `validate_stem` passes it because the header looks fine. Full
    # error string:
    #   "Expected 2D or 3D (batch mode) tensor with possibly 0 batch
    #    size and other non-zero dimensions for input, but got: [1, 2, 0]"
    # Retries ate ~32s of sleeps × 3 attempts before falling through.
    # Aborting on attempt 1 cuts the wasted wallclock by ~90%.
    "expected 2d or 3d",
    "batch mode",
    "got: [1, 2, 0]",
    "non-zero dimensions",
)


def is_non_retryable(err: Exception) -> bool:
    """True if `err`'s message contains a known deterministic-failure
    fragment."""
    msg = str(err).lower()
    return any(f in msg for f in NON_RETRYABLE_FRAGMENTS)


def call_with_budget(
    model: str,
    input_factory: Callable[[], dict],
    *,
    total_budget_s: float,
    backoff: list,
    call_label: str = "replicate",
):
    """Call `replicate.run(model, input=input_factory())` with a global
    wall-clock budget and typed-error short-circuit. Returns the model
    output on success, or None on abort (budget exhausted, non-retryable
    error, or all attempts failed).

    `input_factory` is a zero-arg callable that returns a fresh `input`
    dict each call — the audio file handle is consumed on each upload,
    so we can't reuse a single dict across retries.

    `call_label` is a short identifier (`forced`, `whisperx`, `demucs`)
    used as a log prefix and a Sentry breadcrumb category. Helps trace
    which path burned the budget when reading logs.
    """
    import replicate
    deadline = _t.monotonic() + total_budget_s
    last_err = None
    for attempt, sleep_s in enumerate(backoff):
        if sleep_s > 0:
            if _t.monotonic() + sleep_s >= deadline:
                logger.warning("[%s] sleep %ss would exceed budget — aborting at attempt %s",
                               call_label, sleep_s, attempt + 1)
                break
            _t.sleep(sleep_s)
        if _t.monotonic() >= deadline:
            logger.warning("[%s] budget exhausted before attempt %s/%s",
                           call_label, attempt + 1, len(backoff))
            break
        # File-handle hygiene (2026-05-25 audit): `input_factory()` returns
        # a dict whose values often include `open(audio_path, "rb")` file
        # handles. Replicate's SDK uploads from them but doesn't guarantee
        # to close them when done — and on retry/exception they leak
        # entirely. With concurrent jobs that exhausted the worker's file
        # descriptor limit ("Too many open files" outages). We collect any
        # value that implements `.close()` and close them in `finally`
        # after the call, win or lose.
        _input = input_factory()
        _closables = [v for v in _input.values() if hasattr(v, "close") and callable(v.close)]
        try:
            return replicate.run(model, input=_input)
        except Exception as e:
            last_err = e
            if is_non_retryable(e):
                logger.warning("[%s] non-retryable error on attempt %s (%s) — aborting",
                               call_label, attempt + 1, e)
                return None
            logger.warning("[%s] attempt %s/%s failed (%s)",
                           call_label, attempt + 1, len(backoff), e)
        finally:
            for _h in _closables:
                try:
                    _h.close()
                except Exception:                # already closed / read-only buffer / etc.
                    pass
    logger.warning("[%s] exhausted attempts/budget — last_err=%s", call_label, last_err)
    return None


def _budget_for(label: str, default_s: float = 360.0) -> float:
    """Read the per-call budget from env or fall back to `default_s`.

    Naming: `REPLICATE_BUDGET_S_<LABEL_UPPER>` lets ops tune one path
    without touching the others (e.g. `REPLICATE_BUDGET_S_WHISPERX=600`
    if whisperX legitimately needs more on long-form audio)."""
    var = f"REPLICATE_BUDGET_S_{label.upper()}"
    try:
        return float(os.environ.get(var, default_s))
    except (TypeError, ValueError):
        return default_s
