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
import re
import time as _t
from typing import Any, Callable, Optional

logger = logging.getLogger("genly.replicate_budget")


# Demucs/tqdm prints progress lines like "  35%|███  | 7/20" — pull the first
# integer-percentage we find. Falls back to (done/total) ratio if no `%`
# appears (some forks print "Processing chunk 3 of 8" without ANSI).
_PROGRESS_RE_PCT = re.compile(r"(\d{1,3})\s*%")
_PROGRESS_RE_FRAC = re.compile(r"\b(\d+)\s*/\s*(\d+)\b")


def _parse_progress_from_logs(logs: str) -> Optional[float]:
    """Best-effort extract a 0..1 fraction from a Replicate prediction's
    streamed logs. Returns None if we can't find anything meaningful.

    The model's logs are free text — Demucs (cjwbw) prints a tqdm bar that
    looks like `35%|███   |` so we look for the LAST `%` token (newest
    progress line). If nothing matches, try a `done/total` fraction.
    """
    if not logs:
        return None
    pct_matches = _PROGRESS_RE_PCT.findall(logs)
    if pct_matches:
        try:
            pct = int(pct_matches[-1])
            return max(0.0, min(1.0, pct / 100.0))
        except ValueError:
            pass
    frac_matches = _PROGRESS_RE_FRAC.findall(logs)
    if frac_matches:
        try:
            done, total = (int(x) for x in frac_matches[-1])
            if total > 0:
                return max(0.0, min(1.0, done / total))
        except ValueError:
            pass
    return None


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


def start_replicate_provenance(model: str, call_label: str, attempt: int):
    """Start one provenance row for one actual Replicate SDK attempt.

    The active job comes from the worker context, which ``asyncio.to_thread``
    propagates automatically. Tool names omit the version suffix so they
    match the calibrated Replicate rate table; the pinned hash is retained in
    ``tool_version``. Best-effort like every other provenance write.
    """
    try:
        from observability import current_job_log_context
        job_id = current_job_log_context()
        if not job_id:
            return None
        from provenance import record_ai_call
        tool_name, separator, tool_version = model.partition(":")
        label = (call_label or "replicate").strip().lower()
        return record_ai_call(
            job_id,
            f"replicate_{label}",
            tool_name,
            "replicate",
            f"Replicate prediction ({label}), attempt {attempt}",
            input_data_types=["audio"],
            tool_version=tool_version if separator else None,
        )
    except Exception as exc:
        logger.warning(
            "[%s] could not start Replicate provenance: %s",
            call_label,
            exc,
        )
        return None


def finish_replicate_provenance(recorder, summary: str) -> None:
    """Finish an optional recorder without affecting provider execution."""
    if recorder is None:
        return
    try:
        recorder.finish(response_summary=summary)
    except Exception as exc:
        logger.warning("Could not finish Replicate provenance: %s", exc)


def _run_predict_with_progress(
    model: str,
    inputs: dict,
    *,
    deadline: float,
    call_label: str,
    on_progress: Callable[[float], None],
    poll_interval_s: float = 3.0,
    typical_runtime_s: float = 90.0,
) -> Any:
    """Replacement for `replicate.run()` that pollea the prediction so
    callers can render a moving progress bar while the model runs.

    Strategy for the 0..1 fraction we emit:
      1. As soon as the prediction is created → 0.05 (signals "started").
      2. While `status == "starting"` (Replicate cold-start) → stay at 0.05.
      3. While `status == "processing"`:
         - parse logs for a `%` token (Demucs uses tqdm) → use that.
         - fall back to `elapsed / typical_runtime_s` capped at 0.92.
      4. On `status == "succeeded"` → 1.0 (caller flips to the next stage).

    Returns the raw model output (`prediction.output`), matching the shape
    `replicate.run()` would have returned, so callers don't need to unwrap.

    Raises any exception Replicate raises so the outer `call_with_budget`
    retry machinery can decide whether to retry or short-circuit.
    """
    import replicate
    # Model is `owner/name:version`; predictions.create needs the version
    # hash, while `replicate.run` accepts both. Split here defensively.
    version = model.split(":", 1)[1] if ":" in model else model
    prediction = replicate.predictions.create(version=version, input=inputs)
    start = _t.monotonic()
    last_emitted = -1.0

    def _emit(fraction: float) -> None:
        # Throttle: only emit when fraction moves ≥1% or 5s has passed,
        # to keep DB write rate bounded without dropping the final tick.
        nonlocal last_emitted
        f = max(0.0, min(1.0, fraction))
        if last_emitted < 0 or abs(f - last_emitted) >= 0.01 or f >= 1.0:
            try:
                on_progress(f)
            except Exception as e:                  # callback must NEVER kill the job
                logger.warning("[%s] on_progress raised (%s) — continuing", call_label, e)
            last_emitted = f

    _emit(0.05)
    while True:
        if _t.monotonic() >= deadline:
            try:
                prediction.cancel()
            except Exception:
                pass
            raise TimeoutError(f"{call_label} exceeded wall-clock budget")
        _t.sleep(poll_interval_s)
        try:
            prediction.reload()
        except Exception as e:
            logger.warning("[%s] prediction.reload failed (%s) — keeping last state",
                           call_label, e)
            continue
        status = getattr(prediction, "status", "") or ""
        if status in ("succeeded",):
            _emit(1.0)
            return prediction.output
        if status in ("failed", "canceled"):
            err = getattr(prediction, "error", None) or f"prediction {status}"
            raise RuntimeError(str(err))
        # status in ("starting", "processing")
        elapsed = _t.monotonic() - start
        log_frac = _parse_progress_from_logs(getattr(prediction, "logs", "") or "")
        if log_frac is not None:
            _emit(max(0.05, log_frac))
        else:
            # Time-based fallback, asymptotes near 0.92 so we never claim
            # "done" before the model actually says so. Shape: a slow
            # approach to 0.92 (1 - e^{-t/typical}).
            import math
            approx = 0.92 * (1.0 - math.exp(-elapsed / max(1.0, typical_runtime_s)))
            _emit(max(0.05, approx))


def _noop_progress(_fraction: float) -> None:
    """Progress sink for callers that don't render a bar.

    Existe para que `call_with_budget` pueda mandar a TODOS los callers por
    `_run_predict_with_progress` — el único camino que hace cumplir el
    deadline — sin obligarlos a inventar un callback.
    """


def call_with_budget(
    model: str,
    input_factory: Callable[[], dict],
    *,
    total_budget_s: float,
    backoff: list,
    call_label: str = "replicate",
    on_progress: Optional[Callable[[float], None]] = None,
    typical_runtime_s: float = 90.0,
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

    `on_progress` (optional): callback `(fraction: float) -> None` called
    with 0..1 as the model runs. Demucs uses this to keep the "Aislando
    voz" bar moving instead of stuck at 25%.

    NOTA (incidente 2026-08-26/28): omitir `on_progress` NO cambia el
    enforcement del presupuesto. Siempre usamos `predictions.create +
    poll`, que es el único camino que corta en `deadline` y hace
    `prediction.cancel()`; los callers sin barra reciben un no-op. El
    viejo atajo `replicate.run()` sólo sobrevive para modelos sin version
    hash, y ahí el presupuesto NO se hace cumplir dentro de la llamada.
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
        recorder = start_replicate_provenance(model, call_label, attempt + 1)
        try:
            # INCIDENT 2026-08-26/28 (UMG Chile): `total_budget_s` SÓLO se
            # hacía cumplir dentro de `_run_predict_with_progress` (que
            # pollea y hace `prediction.cancel()` al cruzar el deadline).
            # El camino `replicate.run()` es un `prediction.wait()` — un
            # `while status not in (...): sleep(); reload()` SIN timeout
            # global — así que el presupuesto quedaba en decoración y sólo
            # se chequeaba ENTRE intentos. Consecuencias medidas en prod:
            #   - whisperX: una sola llamada de 1.589.943 ms (26,5 min) con
            #     total_budget_s=480 → se comió el TRANSCRIBE_JOB_TIMEOUT.
            #   - demucs desde los post-pases (CTC/anchor/adlib): el
            #     `asyncio.wait_for(asyncio.to_thread(...), 360)` de arriba
            #     abandona la ESPERA pero no puede cancelar el thread; el
            #     thread seguía poll-eando sin límite y bloqueaba el
            #     `loop.shutdown_default_executor()` del teardown de
            #     `asyncio.run` (que en Python 3.11 no acepta timeout),
            #     matando por death-penalty transcripciones YA terminadas.
            # Ahora TODOS los callers pasan por el camino con deadline; el
            # que no quiere barra de progreso pasa un no-op.
            if ":" in model:
                result = _run_predict_with_progress(
                    model, _input,
                    deadline=deadline,
                    call_label=call_label,
                    on_progress=on_progress or _noop_progress,
                    typical_runtime_s=typical_runtime_s,
                )
            else:
                # `predictions.create` necesita el version hash; un modelo
                # sin pinear (override por env) no puede usar ese camino.
                # Conservamos el histórico y avisamos que va sin tope real.
                logger.warning(
                    "[%s] model %r has no version hash — falling back to the "
                    "UNBOUNDED replicate.run path (budget not enforced "
                    "within the call)", call_label, model,
                )
                result = replicate.run(model, input=_input)
            finish_replicate_provenance(recorder, "succeeded")
            return result
        except Exception as e:
            finish_replicate_provenance(
                recorder,
                f"error: {type(e).__name__}: {str(e)[:300]}",
            )
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
