"""Build human-operated suggestion batches without granting auto-mutation."""
from __future__ import annotations

import math
from typing import Any, Mapping, Sequence


_CONFIDENCE_ORDER = {"high": 0, "medium": 1, "low": 2}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _candidate_window(
    raw: Mapping[str, Any], *, complete_parent_ids: set[str],
) -> dict[str, Any] | None:
    kind = str(raw.get("kind") or "")
    if kind == "operator_review_candidate":
        suggestion_type = str(raw.get("suggestion_type") or "timing")
    elif kind == "review_proposal_candidate":
        parent_id = str(raw.get("parent_window_id") or "")
        if parent_id not in complete_parent_ids:
            return None
        reasons = {str(item) for item in (raw.get("reasons") or [])}
        suggestion_type = (
            "vocalization" if "vocalization" in reasons else "text"
        )
        families = sorted({
            str(item).strip().lower()
            for item in (raw.get("source_families") or []) if str(item).strip()
        })
        if len(families) < 2:
            return None
    else:
        return None
    if suggestion_type not in {"timing", "text", "vocalization"}:
        return None
    current = [dict(item) for item in (raw.get("current_segments") or [])
               if isinstance(item, Mapping)]
    proposed = [dict(item) for item in (raw.get("proposed_segments") or [])
                if isinstance(item, Mapping)]
    # A genuine voiced gap has no editor row to replace. Empty current rows
    # are valid only for review-only text/vocalization insertions; timing must
    # always point at an existing line.
    if not proposed or (not current and suggestion_type == "timing"):
        return None
    start = _number(raw.get("start"), -1.0)
    end = _number(raw.get("end"), -1.0)
    if start < 0 or end <= start:
        return None
    confidence = str(raw.get("confidence") or "high").lower()
    if confidence not in _CONFIDENCE_ORDER:
        confidence = "medium"
    impact_ms = max(0, min(3_600_000, int(_number(
        raw.get("impact_ms"), (end - start) * 1000,
    ))))
    return {
        "kind": "review_proposal_window",
        "schema": "lyrics-quality-v6-review-proposal-window-v1",
        "id": str(raw.get("id") or raw.get("parent_window_id") or ""),
        "start": start,
        "end": end,
        "reasons": sorted({str(item) for item in (raw.get("reasons") or []) if item}),
        "current_segments": current,
        "proposed_segments": proposed,
        "suggestion_type": suggestion_type,
        "confidence": confidence,
        "impact_ms": impact_ms,
        "current_end": raw.get("current_end"),
        "proposed_end": raw.get("proposed_end"),
        "preview_start": raw.get("preview_start", max(0.0, start - 1.0)),
        "preview_end": raw.get("preview_end", end + 1.0),
        "source_families": sorted({
            str(item).strip() for item in (raw.get("source_families") or [])
            if str(item).strip()
        }),
        "selector_policy": str(raw.get("selector_policy") or "independent-consensus-v1"),
        "automatic_apply_allowed": False,
    }


def build_operator_review_proposal(
    segments: Sequence[dict[str, Any]],
    *,
    timing_candidates: Sequence[dict[str, Any]] = (),
    text_candidates: Sequence[dict[str, Any]] = (),
    complete_parent_ids: set[str] | None = None,
    maximum_windows: int = 64,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Select non-overlapping one-click suggestions, ordered by usefulness."""

    complete = set(complete_parent_ids or set())
    windows = [
        window for raw in [*timing_candidates, *text_candidates]
        if (window := _candidate_window(raw, complete_parent_ids=complete)) is not None
    ]
    windows.sort(key=lambda item: (
        _CONFIDENCE_ORDER[item["confidence"]],
        -int(item["impact_ms"]),
        0 if item["suggestion_type"] == "timing" else 1,
        float(item["start"]), str(item["id"]),
    ))
    selected: list[dict[str, Any]] = []
    declined_overlap = 0
    seen_ids: set[str] = set()
    for window in windows:
        if not window["id"] or window["id"] in seen_ids:
            continue
        if any(
            window["start"] < other["end"] - 1e-4
            and window["end"] > other["start"] + 1e-4
            for other in selected
        ):
            declined_overlap += 1
            continue
        selected.append(window)
        seen_ids.add(window["id"])
        if len(selected) >= maximum_windows:
            break
    selected.sort(key=lambda item: (float(item["start"]), float(item["end"])))
    telemetry = {
        "candidate_count": len(windows),
        "proposal_count": len(selected),
        "declined_overlap_count": declined_overlap,
        "by_type": {
            kind: sum(item["suggestion_type"] == kind for item in selected)
            for kind in ("timing", "text", "vocalization")
        },
        "automatic_apply_allowed": False,
    }
    if not selected:
        return None, telemetry
    return {
        "kind": "operator_review_proposal",
        "schema": "operator-review-proposal-v1",
        "policy_version": "human-one-click-suggestions-v1",
        "review_only": True,
        "operator_suggestion_only": True,
        "automatic_apply_allowed": False,
        "windows": selected,
    }, telemetry
