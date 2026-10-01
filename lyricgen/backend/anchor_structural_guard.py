"""Guardrails around re-anchoring and bulk segment writes.

Incident 2026-09-13 (campaña UMG Argentina, staging): a script with an
admin token replaced the lyrics of 185 drafts through ``/save-segments``
(one job per second, evenly spread placeholder timings) and the operator
then re-synced a handful of them. The text was a *longer* version of each
song (Color Esperanza: Diego Torres' full lyric over Coti's 3:42 recording),
so ``forced_align`` — the CTC path staging runs with
``CTC_ALIGN_SKIP_ARCS=0`` — crammed the ~30 lines that are never sung into
0.3-0.8 s slots with word scores ≈ 0. Nothing declined: the aligner's
structural guard only counts skip-arc skips, and ``looks_collapsed`` only
looks at lines shorter than 0.15 s.

Two pure helpers live here so both can be unit-tested without FastAPI:

* :func:`crammed_line_verdict` — post-alignment acoustic check. A run of
  consecutive multi-word lines that are shorter than a second *and* score
  near zero is text the audio does not contain; the re-anchor must decline
  and keep the operator's segments intact.
* :func:`segment_write_velocity` — how many distinct jobs one user wrote
  segments to inside a sliding window. Humans edit a handful of songs per
  ten minutes; the bot wrote 185 in three.
"""

from __future__ import annotations

import os
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def crammed_guard_enabled() -> bool:
    return os.environ.get("REANCHOR_CRAMMED_GUARD", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _line_median_score(segment: Mapping[str, Any]) -> float | None:
    scores = [
        float(w.get("score"))
        for w in (segment.get("words") or [])
        if isinstance(w, Mapping) and isinstance(w.get("score"), (int, float))
    ]
    return statistics.median(scores) if scores else None


def crammed_line_verdict(
    segments: Iterable[Mapping[str, Any]],
    *,
    max_line_s: float | None = None,
    max_score: float | None = None,
    min_words: int = 2,
    min_run: int | None = None,
    max_frac: float | None = None,
    min_words_per_s: float | None = None,
) -> dict[str, Any]:
    """Detect lines the aligner squeezed in because the audio never sings them.

    A line counts as *crammed* when it has at least ``min_words`` words, lasts
    less than ``max_line_s`` seconds and EITHER its median word score is below
    ``max_score`` OR it packs ``min_words_per_s`` words per second or more.
    The second test needs no scores: the Whisper-DP fallback pads every line
    it could not anchor to a fixed 0.6 s and stamps a constant 0.6 score on
    whatever words fall inside (Buseca y Vino Tinto, 2026-09-14: 22 of 51
    lines interpolated, runs of five 0.6 s lines with five words each), so a
    score-only test waved it through. The verdict is a structural mismatch
    when ``min_run`` or more crammed lines are consecutive, or when crammed
    lines are at least ``max_frac`` of the lines that could be judged.

    Calibrated on the 2026-09-13 campaign snapshot (311 live jobs): Color
    Esperanza (run 7, 31 %), Zi Zi Zi (run 6, 29 %) and the Buseca fallback
    (run 5, 29 %) trip on the RUN test; the only healthy-looking job with a
    run of 4 is a forced_align medley that is itself broken. The fraction
    test is a backstop for scattered cramming and sits at 0.4 because
    hand-edited songs with many short lines legitimately reach 0.25-0.28.
    Pure; never raises on odd input.
    """
    max_line_s = _env_float("REANCHOR_CRAMMED_MAX_LINE_S", 1.0) if max_line_s is None else max_line_s
    max_score = _env_float("REANCHOR_CRAMMED_MAX_SCORE", 0.1) if max_score is None else max_score
    min_run = _env_int("REANCHOR_CRAMMED_MIN_RUN", 4) if min_run is None else min_run
    max_frac = _env_float("REANCHOR_CRAMMED_MAX_FRAC", 0.4) if max_frac is None else max_frac
    min_words_per_s = (
        _env_float("REANCHOR_CRAMMED_MIN_WORDS_PER_S", 4.0)
        if min_words_per_s is None else min_words_per_s
    )

    crammed_idx: list[int] = []
    scored = 0
    total = 0
    for idx, seg in enumerate(segments):
        if not isinstance(seg, Mapping):
            continue
        total += 1
        try:
            dur = float(seg.get("end") or 0.0) - float(seg.get("start") or 0.0)
        except (TypeError, ValueError):
            continue
        n_words = len(str(seg.get("text") or "").split())
        if n_words < min_words:
            continue
        scored += 1
        if dur >= max_line_s:
            continue
        med = _line_median_score(seg)
        low_score = med is not None and med < max_score
        too_dense = n_words / max(dur, 0.01) >= min_words_per_s
        if low_score or too_dense:
            crammed_idx.append(idx)

    run = best = 0
    prev = -2
    for idx in crammed_idx:
        run = run + 1 if idx == prev + 1 else 1
        best = max(best, run)
        prev = idx
    frac = (len(crammed_idx) / scored) if scored else 0.0
    mismatch = bool(crammed_idx) and (best >= min_run or frac >= max_frac)
    return {
        "mismatch": mismatch,
        "crammed_lines": len(crammed_idx),
        "crammed_run": best,
        "crammed_fraction": round(frac, 3),
        "scored_lines": scored,
        "total_lines": total,
        "crammed_indices": crammed_idx[:50],
    }


def velocity_limits() -> tuple[int, int]:
    """(max distinct jobs, window seconds). ``0`` jobs disables the guard."""
    return (
        _env_int("SEGMENT_WRITE_MAX_DISTINCT_JOBS", 25),
        _env_int("SEGMENT_WRITE_WINDOW_S", 600),
    )


def segment_write_velocity(
    db, user_id: int | None, job_id: str, *, window_s: int, limit: int = 2000,
) -> int:
    """Distinct jobs ``user_id`` wrote segments to within ``window_s`` seconds,
    counting ``job_id`` itself. Reads ``audit_log`` rows of action
    ``lyrics.segments_diff`` (emitted by every content-changing save, legacy
    and Editor 2.0 alike). Never raises: a failing query counts as 1.
    """
    if user_id is None:
        return 1
    try:
        from database import AuditLog
        since = datetime.now(timezone.utc) - timedelta(seconds=max(1, int(window_s)))
        rows = (
            db.query(AuditLog.detail)
            .filter(
                AuditLog.user_id == user_id,
                AuditLog.action == "lyrics.segments_diff",
                AuditLog.created_at >= since,
            )
            .order_by(AuditLog.id.desc())
            .limit(limit)
            .all()
        )
    except Exception:
        return 1
    jobs = {str(job_id)}
    for (detail,) in rows:
        if isinstance(detail, Mapping):
            jid = detail.get("job_id")
            if jid:
                jobs.add(str(jid))
    return len(jobs)


def segment_write_velocity_exceeded(db, user_id: int | None, job_id: str) -> tuple[bool, int, int]:
    """(exceeded, distinct_jobs, max_jobs) for the configured window."""
    max_jobs, window_s = velocity_limits()
    if max_jobs <= 0:
        return False, 0, max_jobs
    count = segment_write_velocity(db, user_id, job_id, window_s=window_s)
    return count > max_jobs, count, max_jobs
