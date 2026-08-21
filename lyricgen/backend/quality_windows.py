"""Typed tiling contract shared by quality analysis and provider retries."""

from __future__ import annotations

import hashlib
import json
import math
from typing import TypedDict


_TIME_PRECISION = 6


class QualityWindowStats(TypedDict, total=False):
    """Canonical retry contract; unknown/truncated coverage fails closed."""

    windows_considered: int
    windows_processed: int
    windows_skipped: int
    windows_truncated: int
    parent_coverage: dict[str, dict]


def _window_id(window: dict, index: int) -> str:
    if window.get("id"):
        return str(window["id"])
    stable = json.dumps(
        [window.get("start"), window.get("end"), window.get("reasons")],
        sort_keys=True, separators=(",", ":"), default=str,
    )
    return f"window-{index}-{hashlib.sha256(stable.encode()).hexdigest()[:10]}"


def tile_unsafe_windows(
    windows: list[dict], *, core_seconds: float = 24.0,
    context_seconds: float = 3.0,
    audio_duration: float | None = None,
) -> list[dict]:
    """Cover every parent with bounded overlapping analysis tiles.

    `core_start/core_end` partition the requested parent exactly when it fits
    inside the media. `start/end` include context and may overlap. When
    ``audio_duration`` is supplied (or a window carries its own duration),
    bounds are clamped to real media and an overhanging parent is explicitly
    marked truncated. A consumer may diagnose that bounded portion, but it
    must never mark the parent resolved.
    """
    core_seconds = max(1.0, min(float(core_seconds), 45.0))
    context_seconds = max(0.0, min(float(context_seconds), 10.0))

    def duration_bound(value: object) -> float | None:
        if value is None or value == "":
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("audio_duration must be a finite positive number") from exc
        if not math.isfinite(parsed) or parsed <= 0:
            raise ValueError("audio_duration must be a finite positive number")
        return parsed

    global_audio_end = duration_bound(audio_duration)
    tiled: list[dict] = []
    for index, raw in enumerate(windows or []):
        audio_end = global_audio_end
        if audio_end is None:
            audio_end = duration_bound(raw.get("audio_duration"))
        try:
            requested_start = max(0.0, float(raw.get("start") or 0.0))
            requested_end = float(raw.get("end") or requested_start)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(requested_start) or not math.isfinite(requested_end):
            continue
        parent_start, parent_end = requested_start, requested_end
        duration_truncated = bool(
            audio_end is not None and requested_end > audio_end + 1e-9
        )
        inherited_truncation = bool(raw.get("analysis_truncated"))
        analysis_truncated = duration_truncated or inherited_truncation
        if audio_end is not None:
            parent_start = min(parent_start, audio_end)
            parent_end = min(parent_end, audio_end)
        if parent_end <= parent_start:
            continue
        parent_id = _window_id(raw, index)
        cores = []
        cursor = parent_start
        while cursor < parent_end - 1e-9:
            core_end = min(parent_end, cursor + core_seconds)
            cores.append((cursor, core_end))
            cursor = core_end
        count = len(cores)
        for tile_index, (core_start, core_end) in enumerate(cores):
            tiled.append({
                **raw,
                "id": f"{parent_id}:tile:{tile_index + 1}:{count}",
                "parent_window_id": parent_id,
                "tile_index": tile_index,
                "tile_count": count,
                "core_start": round(core_start, _TIME_PRECISION),
                "core_end": round(core_end, _TIME_PRECISION),
                "start": round(
                    max(0.0, core_start - context_seconds), _TIME_PRECISION,
                ),
                "end": round(min(
                    core_end + context_seconds,
                    audio_end if audio_end is not None else math.inf,
                ), _TIME_PRECISION),
                "audio_duration": audio_end,
                "coverage_complete": bool(
                    raw.get("coverage_complete", True)
                    and not analysis_truncated
                ),
                "analysis_truncated": analysis_truncated,
            })
    return tiled


def parent_coverage(tiles: list[dict], processed_tile_ids: set[str]) -> dict[str, dict]:
    groups: dict[str, list[dict]] = {}
    for tile in tiles:
        groups.setdefault(str(tile.get("parent_window_id") or tile.get("id")), []).append(tile)
    result = {}
    for parent_id, siblings in groups.items():
        processed = sum(str(tile.get("id")) in processed_tile_ids for tile in siblings)
        truncated = sum(bool(tile.get("analysis_truncated")) for tile in siblings)
        coverage_declined = any(
            not bool(tile.get("coverage_complete", True)) for tile in siblings
        )
        item = {
            "tiles_total": len(siblings), "tiles_processed": processed,
            "complete": (
                processed == len(siblings)
                and truncated == 0
                and not coverage_declined
            ),
        }
        if truncated or coverage_declined:
            item["truncated"] = bool(truncated)
            item["tiles_truncated"] = truncated
        result[parent_id] = item
    return result
