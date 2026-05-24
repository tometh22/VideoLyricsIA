"""Bug-detector registry for the QA suite.

Each detector is a pure function `(run, expected, audio_duration_s) -> CheckResult | None`.
Return None when the detector doesn't apply (e.g., missing field). Detectors
share the `Status` / `CheckResult` types with the preflight suite.

The whole value of the QA suite lives in this file: every detector is a
specific historical incident or a known bug pattern we never want to ship
silently again.
"""
from __future__ import annotations

import statistics
import sys
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

# Reuse preflight types so QA output is interoperable with daily smoke.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # backend/scripts/
from preflight._base import Status, CheckResult  # noqa: E402


Detector = Callable[[dict, dict, Optional[float]], Optional[CheckResult]]


def _name(fn: Callable) -> str:
    return getattr(fn, "__name__", "unknown")


def _segs(run: dict) -> list[dict]:
    return run.get("segments") or []


# ─── FAIL-severity detectors ──────────────────────────────────────────────

def timing_source_in_allowlist(run, expected, audio_duration_s):
    """Actual timing_source must be in expected.timing_source allow-list."""
    allow = expected.get("timing_source")
    if not allow:
        return None
    actual = run.get("source")
    if actual in allow:
        return CheckResult(_name(timing_source_in_allowlist), Status.PASS,
                           f"timing_source={actual!r} OK")
    return CheckResult(_name(timing_source_in_allowlist), Status.FAIL,
                       f"timing_source={actual!r} not in {allow}",
                       {"actual": actual, "allowed": allow})


def timing_source_not_in_deny(run, expected, audio_duration_s):
    """Regression guard after PR-G WorldClass — lrclib_synced must NEVER be
    the timing_source. The whole audio-as-truth promise rests on this."""
    deny = expected.get("forbidden_timing_sources") or []
    actual = run.get("source")
    if actual in deny:
        return CheckResult(_name(timing_source_not_in_deny), Status.FAIL,
                           f"FORBIDDEN timing_source={actual!r} (PR-G regression)",
                           {"actual": actual, "forbidden": deny})
    return None  # passes silently


def mega_segment_hallucination(run, expected, audio_duration_s):
    """Match the "El Arbol" incident: a single segment with dur > 30s
    AND words-per-second < 0.1 means Whisper bailed and emitted one
    long held phrase ("Música de presentación" 346s/3 words)."""
    for s in _segs(run):
        try:
            dur = float(s.get("end", 0)) - float(s.get("start", 0))
        except (TypeError, ValueError):
            continue
        n_words = len((s.get("text") or "").split())
        if dur > 30 and (n_words / max(dur, 1)) < 0.1:
            return CheckResult(_name(mega_segment_hallucination), Status.FAIL,
                               f"mega-segment {dur:.0f}s/{n_words}w (El Arbol shape)",
                               {"start": s.get("start"), "end": s.get("end"),
                                "text": (s.get("text") or "")[:60]})
    return None


def uniform_synthesizer_fallback(run, expected, audio_duration_s):
    """Detect the "Cosas Mías" original symptom: lines distributed evenly
    across the audio (variance of inter-segment gaps near zero) = the
    `_synthesize_segments_from_plain` fallback fired without real anchors."""
    segs = _segs(run)
    if len(segs) < 8:
        return None
    try:
        starts = [float(s["start"]) for s in segs]
    except (TypeError, ValueError, KeyError):
        return None
    gaps = [starts[i + 1] - starts[i] for i in range(len(starts) - 1)]
    if not gaps:
        return None
    var = statistics.pvariance(gaps)
    if var < 0.5:
        return CheckResult(_name(uniform_synthesizer_fallback), Status.FAIL,
                           f"inter-segment-gap variance {var:.2f} — uniform fallback shape",
                           {"variance": round(var, 3), "n_segments": len(segs),
                            "mean_gap_s": round(statistics.mean(gaps), 2)})
    return None


def max_line_dur_exceeded(run, expected, audio_duration_s):
    """No line should hold longer than expected.max_line_dur_s (8s default).
    Rotor lines run ~3-5s; long lines kill readability."""
    ceiling = float(expected.get("max_line_dur_s") or 8.0)
    longest = None
    for s in _segs(run):
        try:
            dur = float(s.get("end", 0)) - float(s.get("start", 0))
        except (TypeError, ValueError):
            continue
        if longest is None or dur > longest[0]:
            longest = (dur, s)
    if longest and longest[0] > ceiling:
        return CheckResult(_name(max_line_dur_exceeded), Status.FAIL,
                           f"longest line {longest[0]:.1f}s > ceiling {ceiling:.1f}s",
                           {"start": longest[1].get("start"),
                            "text": (longest[1].get("text") or "")[:60]})
    return None


def missing_word_stamps(run, expected, audio_duration_s):
    """When expected.must_have_words is true, at least one segment must
    carry a `words` list — required for karaoke + confidence highlighting."""
    if not expected.get("must_have_words"):
        return None
    segs = _segs(run)
    has = any((s.get("words") or []) for s in segs)
    if has:
        return None
    return CheckResult(_name(missing_word_stamps), Status.FAIL,
                       "no segment carries per-word stamps",
                       {"n_segments": len(segs),
                        "timing_source": run.get("source")})


def segment_past_audio_end(run, expected, audio_duration_s):
    """No segment may end past the audio duration (+ 0.5s tolerance)."""
    if not audio_duration_s:
        return None
    for s in _segs(run):
        try:
            end = float(s.get("end", 0))
        except (TypeError, ValueError):
            continue
        if end > audio_duration_s + 0.5:
            return CheckResult(_name(segment_past_audio_end), Status.FAIL,
                               f"segment ends at {end:.1f}s > audio_dur {audio_duration_s:.1f}s",
                               {"end": end, "audio_duration_s": audio_duration_s,
                                "text": (s.get("text") or "")[:60]})
    return None


def first_vocal_too_early_for_long_audio(run, expected, audio_duration_s):
    """Long song (>240s) with first vocal < 2s usually means uniform
    fallback distributed lines from t=0. Suspicious unless timing came
    from lrclib (which legitimately can start at 0)."""
    if not audio_duration_s or audio_duration_s <= 240:
        return None
    segs = _segs(run)
    if not segs:
        return None
    try:
        first = float(segs[0].get("start", 0))
    except (TypeError, ValueError):
        return None
    source = (run.get("source") or "")
    if first < 2.0 and not source.startswith("lrclib_"):
        return CheckResult(_name(first_vocal_too_early_for_long_audio), Status.FAIL,
                           f"first vocal at {first:.2f}s for {audio_duration_s:.0f}s audio (uniform fallback?)",
                           {"first_start": first, "audio_duration_s": audio_duration_s,
                            "timing_source": source})
    return None


def pipeline_exception(run, expected, audio_duration_s):
    """The runner caught an exception from `_run_transcription_for_job`."""
    err = (run.get("meta") or {}).get("error")
    if err:
        return CheckResult(_name(pipeline_exception), Status.FAIL,
                           f"pipeline raised: {err[:120]}",
                           {"error": err})
    return None


# ─── WARN-severity detectors ──────────────────────────────────────────────

def n_lines_in_range(run, expected, audio_duration_s):
    rng = expected.get("n_lines_range")
    if not rng or len(rng) != 2:
        return None
    lo, hi = rng
    n = len(_segs(run))
    if lo <= n <= hi:
        return None
    return CheckResult(_name(n_lines_in_range), Status.WARN,
                       f"n_segments={n} outside [{lo}, {hi}]",
                       {"n_segments": n, "range": rng})


def first_vocal_in_range(run, expected, audio_duration_s):
    rng = expected.get("first_vocal_s_range")
    if not rng or len(rng) != 2:
        return None
    segs = _segs(run)
    if not segs:
        return None
    try:
        first = float(segs[0].get("start", 0))
    except (TypeError, ValueError):
        return None
    lo, hi = rng
    if lo <= first <= hi:
        return None
    return CheckResult(_name(first_vocal_in_range), Status.WARN,
                       f"first_vocal={first:.1f}s outside [{lo}, {hi}]",
                       {"first_start": first, "range": rng})


def missing_repetition_groups(run, expected, audio_duration_s):
    if not expected.get("must_have_repetition_groups"):
        return None
    groups = {s.get("repetition_group") for s in _segs(run) if s.get("repetition_group")}
    if groups:
        return None
    return CheckResult(_name(missing_repetition_groups), Status.WARN,
                       "no repetition_group tagged",
                       {"n_segments": len(_segs(run))})


def elapsed_exceeded(run, expected, audio_duration_s):
    ceiling = expected.get("max_elapsed_s")
    if not ceiling:
        return None
    elapsed = (run.get("telemetry") or {}).get("elapsed_seconds") or run.get("elapsed_seconds")
    if elapsed is None:
        return None
    if elapsed > ceiling:
        return CheckResult(_name(elapsed_exceeded), Status.WARN,
                           f"elapsed {elapsed:.0f}s > ceiling {ceiling}s",
                           {"elapsed_s": elapsed, "ceiling_s": ceiling})
    return None


def replicate_throttle_seen(run, expected, audio_duration_s):
    cost = run.get("cost") or {}
    n = cost.get("throttle_429_count", 0)
    if not n:
        return None
    return CheckResult(_name(replicate_throttle_seen), Status.WARN,
                       f"{n} Replicate 429 throttle(s) seen",
                       {"throttle_429_count": n})


# Ordered for the report: FAIL detectors first, WARNs after.
DETECTORS: list[Detector] = [
    # FAIL
    timing_source_in_allowlist,
    timing_source_not_in_deny,
    mega_segment_hallucination,
    uniform_synthesizer_fallback,
    max_line_dur_exceeded,
    missing_word_stamps,
    segment_past_audio_end,
    first_vocal_too_early_for_long_audio,
    pipeline_exception,
    # WARN
    n_lines_in_range,
    first_vocal_in_range,
    missing_repetition_groups,
    elapsed_exceeded,
    replicate_throttle_seen,
]


def run_all(run: dict, expected: dict, audio_duration_s: float | None) -> list[CheckResult]:
    """Execute every detector; capture crashes as ERROR results so the
    suite never aborts mid-analysis."""
    out: list[CheckResult] = []
    for fn in DETECTORS:
        try:
            r = fn(run, expected, audio_duration_s)
            if r is not None:
                out.append(r)
        except Exception as e:
            out.append(CheckResult(_name(fn), Status.ERROR,
                                   f"detector crashed: {e}",
                                   {"traceback": traceback.format_exc()}))
    return out
