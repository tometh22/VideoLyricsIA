"""Replicate cost estimator. Grep per-song logs for engine markers and
multiply by published per-call costs. Best-effort; absolute costs come
from the Replicate billing dashboard."""
from __future__ import annotations

import re
from pathlib import Path

# Per-call USD estimates (updated 2026-05-22 against actual Replicate spend).
# These match the budget figures we document in env.example and the PR
# descriptions; the live billing dashboard remains the source of truth.
DEMUCS_USD = 0.03
WHISPERX_USD = 0.02
FORCED_ALIGN_USD = 0.007


# Each pattern recognises one *successful or attempted* engine call. We
# count attempts (not only successes) because each attempt is metered.
_PATTERNS = {
    "demucs": [
        re.compile(r"\[VOCALSEP\]\s+isolated vocal stem"),
        # Attempts that failed leave a different log line — counted via
        # the throttle / fail pattern below to avoid double-counting,
        # because cancelled Replicate predictions are usually NOT charged.
    ],
    "whisperx": [
        re.compile(r"\[WHISPERX\]\s+transcribed \d+ segments"),
        re.compile(r"\[WHISPERX\]\s+thin/empty result"),
    ],
    "forced_align": [
        re.compile(r"\[FORCED\]\s+aligned \d+/\d+ lines"),
        # Failed-after-retries is metered for the attempts that ran.
        re.compile(r"\[FORCED\]\s+replicate failed after \d+ retries"),
    ],
}

_THROTTLE_PATTERN = re.compile(r"status:\s*429|burst of 1 requests|Request was throttled")


def estimate_from_log(log_path: str | Path) -> dict:
    """Count engine invocations + 429s from a single song's log file.

    Returns:
        {
          demucs_calls, whisperx_calls, forced_align_calls,
          throttle_429_count,
          usd: {demucs, whisperx, forced_align, total}
        }
    """
    counts = {"demucs": 0, "whisperx": 0, "forced_align": 0}
    throttles = 0
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if _THROTTLE_PATTERN.search(line):
                    throttles += 1
                for kind, patterns in _PATTERNS.items():
                    if any(p.search(line) for p in patterns):
                        counts[kind] += 1
                        break  # at most one engine match per line
    except FileNotFoundError:
        pass

    usd = {
        "demucs": round(counts["demucs"] * DEMUCS_USD, 4),
        "whisperx": round(counts["whisperx"] * WHISPERX_USD, 4),
        "forced_align": round(counts["forced_align"] * FORCED_ALIGN_USD, 4),
    }
    usd["total"] = round(sum(usd.values()), 4)
    return {
        "demucs_calls": counts["demucs"],
        "whisperx_calls": counts["whisperx"],
        "forced_align_calls": counts["forced_align"],
        "throttle_429_count": throttles,
        "usd": usd,
    }


def aggregate(per_song: dict[str, dict]) -> dict:
    """Aggregate per-song cost dicts into a run-level summary."""
    total_calls = {"demucs": 0, "whisperx": 0, "forced_align": 0}
    total_usd = {"demucs": 0.0, "whisperx": 0.0, "forced_align": 0.0, "total": 0.0}
    total_throttles = 0
    for c in per_song.values():
        total_calls["demucs"] += c.get("demucs_calls", 0)
        total_calls["whisperx"] += c.get("whisperx_calls", 0)
        total_calls["forced_align"] += c.get("forced_align_calls", 0)
        total_throttles += c.get("throttle_429_count", 0)
        for k, v in (c.get("usd") or {}).items():
            total_usd[k] = round(total_usd.get(k, 0.0) + (v or 0.0), 4)
    return {
        "calls": total_calls,
        "usd": total_usd,
        "throttle_429_count": total_throttles,
    }
