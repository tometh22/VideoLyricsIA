"""Per-step ETA estimation for the job progress UX.

WHY
---
The pre-2026-05-27 "Construyendo tu video" screen suffered three UX
bugs that made the operator think the system was broken:

  1. The progress bar froze at 22% during the Veo background generation
     (60-180s) because the worker only ticked `last_progress_at` (used
     by the reaper) without updating `progress` itself.
  2. The frontend cycled through ALL pipeline steps every 3.2s
     (`setInterval`), so the user saw "Transcribiendo lyrics" loop into
     view long after the lyrics step was done — looked confused.
  3. The "~8 min restantes" label was a HARDCODED fallback in
     `BatchProgress.jsx:520` used whenever no completed jobs existed
     in the batch to average from. For single-song generates (the
     common case), it ALWAYS said 8 min, regardless of actual progress.

WHAT THIS MODULE DOES
---------------------
Pure functions, no I/O. Given the worker's `current_step` (string) and
`progress` (0-100 int), returns:

  - `compute_eta_s(step, progress) -> int | None`
        Estimated seconds remaining, or None if the step is unknown.
  - `STEP_USER_TEXT_ES`
        Mapping from internal step name → operator-facing Spanish
        phrase. The frontend uses the same dict via the SSE payload
        OR re-implements it via i18n.

The `STEP_TYPICAL_DURATIONS_S` table was calibrated from prod logs
2026-05-22..05-26 (median across 200+ "638"-class single-song
generations). Veo is the dominant cost (~120s); everything else is
secondary.

CONTRACT for new callers
------------------------
SSE payload (`main.py`):
  payload["eta_s"] = step_eta.compute_eta_s(current_step, progress)

Frontend (BatchProgress.jsx):
  - Show step_user_text via i18n keyed on current_step
  - Show "~N min restantes" / "~N seg restantes" from `eta_s`
  - When `eta_s` is None (unknown step), fall back to neutral
    "Procesando..." with no ETA number

Tests live in `tests/test_step_eta.py`.
"""
from __future__ import annotations


# Progress range each step occupies within the 0-100 bar. The worker
# writes `progress=p_start` when entering the step and `progress=p_end`
# (or whatever the next step's p_start is) when leaving it. The
# sub-progress crawl inside `_ensure_background()` ticks `progress`
# within this range while Veo is running.
#
# These ranges are READ FROM `pipeline.py` — if the pipeline changes
# its `update_job(progress=N, current_step="X")` calls, update both.
STEP_PROGRESS_RANGES: dict[str, tuple[int, int]] = {
    "starting":   (1, 5),
    "whisper":    (5, 20),
    "background": (22, 40),
    "validation": (38, 40),
    "video":      (40, 75),
    "short":      (75, 90),
    "thumbnail":  (90, 95),
    "upload":     (95, 100),
}

# Typical wall-clock duration of each step in seconds, calibrated from
# prod logs (median across 200+ single-song generations 2026-05-22..05-26).
# These are intentionally PESSIMISTIC for the user-facing display:
# overestimating ETA is forgivable, underestimating makes the user
# think we're stuck.
STEP_TYPICAL_DURATIONS_S: dict[str, int] = {
    "starting":   5,
    "whisper":    30,
    "background": 120,   # Veo dominant. Imagen path is ~30s; we use
                         # the worst case so the bar never "runs out".
    "validation": 5,
    "video":      30,
    "short":      30,
    "thumbnail":  10,
    "upload":     20,
}

# Order in which steps execute. Used to sum subsequent steps' typical
# durations into the ETA.
STEP_ORDER: list[str] = [
    "starting", "whisper", "background", "validation",
    "video", "short", "thumbnail", "upload",
]

# Operator-facing Spanish phrase for each step. The frontend keeps its
# own copy via i18n (see `i18n.jsx`), but having it here too makes the
# SSE payload self-describing for debug pages or third-party
# consumers (admin dashboards).
STEP_USER_TEXT_ES: dict[str, str] = {
    "starting":   "Preparando…",
    "whisper":    "Preparando…",
    "background": "Generando el fondo cinematográfico",
    "validation": "Validando contenido",
    "video":      "Armando el video",
    "short":      "Generando el Short",
    "thumbnail":  "Creando la miniatura",
    "upload":     "Guardando en tu galería",
}


def compute_eta_s(current_step: str | None, progress: int | None) -> int | None:
    """Return remaining seconds estimated to reach progress=100.

    Args:
        current_step: One of the keys in `STEP_PROGRESS_RANGES`. None or
            unknown returns None (caller shows fallback text).
        progress: 0-100 integer the worker last wrote. None or out-of-
            range gets clamped.

    Returns:
        Non-negative integer seconds, or None when no estimate is
        possible (unknown step). 0 is valid (job is finishing).

    Notes:
        - For the current step, we estimate progress-within-step from
          the `progress` value's position inside `STEP_PROGRESS_RANGES`.
          This works because the sub-progress crawl in
          `_ensure_background()` (and equivalents) ticks `progress`
          while the step runs.
        - For subsequent steps we just sum their typical durations —
          no historical learning yet (could add in v2 if needed).
    """
    if not current_step:
        return None
    step = current_step.strip().lower() if isinstance(current_step, str) else None
    if step not in STEP_PROGRESS_RANGES:
        return None

    p_start, p_end = STEP_PROGRESS_RANGES[step]
    typical = STEP_TYPICAL_DURATIONS_S[step]

    # How far into the current step are we? Clamp to [0, 1].
    if progress is None:
        progress_clamped = p_start
    else:
        try:
            progress_clamped = int(progress)
        except (TypeError, ValueError):
            progress_clamped = p_start
    span = max(1, p_end - p_start)
    fraction_done = max(0.0, min(1.0, (progress_clamped - p_start) / span))

    # Time remaining in current step.
    current_remaining = typical * (1.0 - fraction_done)

    # Plus typical duration of all subsequent steps.
    try:
        idx = STEP_ORDER.index(step)
        subsequent = sum(
            STEP_TYPICAL_DURATIONS_S[s] for s in STEP_ORDER[idx + 1:]
        )
    except ValueError:
        subsequent = 0

    return max(0, int(current_remaining + subsequent))


def format_eta_es(eta_s: int | None) -> str | None:
    """Format an ETA into operator-facing Spanish. Returns None when
    `eta_s` is None (caller suppresses the line entirely)."""
    if eta_s is None:
        return None
    if eta_s <= 0:
        return "Casi listo…"
    if eta_s < 90:
        return f"~{eta_s} seg restantes"
    # Round up to nearest minute so the displayed number doesn't tick
    # backwards weirdly (e.g. 119s → "~2 min" instead of "~1 min" then
    # 60s later "~1 min" still).
    minutes = (eta_s + 30) // 60
    return f"~{minutes} min restantes"


__all__ = [
    "STEP_PROGRESS_RANGES",
    "STEP_TYPICAL_DURATIONS_S",
    "STEP_ORDER",
    "STEP_USER_TEXT_ES",
    "compute_eta_s",
    "format_eta_es",
]
