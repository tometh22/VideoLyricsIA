"""Revision-bound proposals built from label delivery change requests."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import re
from typing import Any, Iterable

from change_request_parser import fold_text, parse_change_request
from editor import normalize_segments, segments_content_hash
from transcription_quality import segments_hash


SCHEMA_VERSION = "change-request-proposal-v1"
MANUAL_KINDS = {
    "manual_review", "timing_review", "structure_review",
    "background_review", "audio_review",
}


def request_hash(comment: str) -> str:
    return hashlib.sha256((comment or "").strip().encode("utf-8")).hexdigest()


def _operation_id(*parts: Any) -> str:
    raw = "|".join(str(part) for part in parts)
    return "cr-op-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _segment_midpoint(row: dict) -> float:
    return (float(row.get("start") or 0) + float(row.get("end") or 0)) / 2


def _at_time(segments: list[dict], value: float) -> tuple[int, dict] | None:
    containing = [
        (index, row) for index, row in enumerate(segments)
        if float(row.get("start") or 0) - 0.20 <= value
        <= float(row.get("end") or 0) + 0.20
    ]
    if containing:
        return min(containing, key=lambda item: abs(_segment_midpoint(item[1]) - value))
    if not segments:
        return None
    nearest = min(
        enumerate(segments), key=lambda item: abs(_segment_midpoint(item[1]) - value),
    )
    return nearest if abs(_segment_midpoint(nearest[1]) - value) <= 2.5 else None


def _replace_text(current: str, expected: str | None, requested: str) -> str | None:
    current = str(current or "")
    requested = str(requested or "").strip()
    if not requested:
        return None
    if not expected:
        return requested
    expected = str(expected).strip()
    if fold_text(current) == fold_text(expected):
        return requested
    # Substring replacement is allowed only when the exact client-supplied
    # text exists. Accent-folded fuzzy containment is review-only because it
    # cannot identify safe character offsets without guessing.
    match = re.search(re.escape(expected), current, flags=re.IGNORECASE)
    if not match:
        return None
    return current[:match.start()] + requested + current[match.end():]


def _text_targets(
    segments: list[dict], instruction: dict,
) -> list[tuple[int, dict, str]]:
    requested = str(instruction.get("requested_text") or "").strip()
    expected = instruction.get("current_text")
    timecode = instruction.get("timecode_seconds")
    scope = instruction.get("scope")

    primary: tuple[int, dict] | None = None
    if timecode is not None:
        primary = _at_time(segments, float(timecode))
    elif expected:
        exact = [
            (index, row) for index, row in enumerate(segments)
            if fold_text(str(row.get("text") or "")) == fold_text(str(expected))
        ]
        if len(exact) == 1:
            primary = exact[0]
        elif scope == "all_matching" and exact:
            primary = exact[0]
    if primary is None:
        return []

    candidates = [primary]
    if scope == "all_matching":
        needle = fold_text(str(expected or primary[1].get("text") or ""))
        candidates = [
            (index, row) for index, row in enumerate(segments)
            if fold_text(str(row.get("text") or "")) == needle
        ]
    targets: list[tuple[int, dict, str]] = []
    for index, row in candidates:
        proposed = _replace_text(str(row.get("text") or ""), expected, requested)
        if proposed is not None and proposed != str(row.get("text") or ""):
            targets.append((index, row, proposed))
    return targets


def _text_operation(
    *, instruction: dict, index: int, current: dict, proposed_text: str,
) -> dict:
    proposed = {**deepcopy(current), "text": proposed_text}
    current_rows = [deepcopy(current)]
    proposed_rows = [proposed]
    group_key = instruction.get("id")
    return {
        "id": _operation_id(
            instruction.get("id"), index, current.get("start"),
            current.get("end"), current.get("text"), proposed_text,
        ),
        "group_key": group_key,
        "kind": "replace_text",
        "status": "pending",
        "applicable": True,
        "automatic_apply_allowed": False,
        "confidence": instruction.get("confidence") or "medium",
        "scope": instruction.get("scope") or "single",
        "source_excerpt": instruction.get("source_excerpt"),
        "timecode_seconds": instruction.get("timecode_seconds"),
        "start": float(current.get("start") or 0),
        "end": float(current.get("end") or 0),
        "current_segments": current_rows,
        "proposed_segments": proposed_rows,
        "current_segments_hash": segments_content_hash(current_rows),
        "proposed_segments_hash": segments_content_hash(proposed_rows),
        "warnings": [],
    }


def _period_operations(segments: list[dict], instruction: dict) -> list[dict]:
    rows: list[dict] = []
    for index, current in enumerate(segments):
        text = str(current.get("text") or "")
        if not re.search(r"\.(?:[\"'”’)]*)\s*$", text):
            continue
        proposed_text = re.sub(r"\.(?=[\"'”’)]*\s*$)", "", text)
        operation = _text_operation(
            instruction=instruction, index=index, current=current,
            proposed_text=proposed_text,
        )
        operation.update({
            "kind": "remove_terminal_period",
            "automatic_apply_allowed": not bool(
                current.get("locked") is True
                or current.get("operator_locked") is True
            ),
        })
        if not operation["automatic_apply_allowed"]:
            operation["warnings"] = ["locked_segment_requires_confirmation"]
        rows.append(operation)
    return rows


def _manual_operation(instruction: dict) -> dict:
    return {
        "id": _operation_id(
            instruction.get("id"), instruction.get("kind"),
            instruction.get("source_excerpt"),
        ),
        "group_key": instruction.get("id"),
        "kind": instruction.get("kind") or "manual_review",
        "status": "pending",
        "applicable": False,
        "automatic_apply_allowed": False,
        "confidence": instruction.get("confidence") or "low",
        "scope": instruction.get("scope") or "single",
        "source_excerpt": instruction.get("source_excerpt"),
        "timecode_seconds": instruction.get("timecode_seconds"),
        "reason": instruction.get("reason") or "manual_review_required",
        "current_segments": [],
        "proposed_segments": [],
        "warnings": [instruction.get("reason") or "manual_review_required"],
    }


def build_proposal(
    *, comment: str, segments: Iterable[dict], base_revision: int,
    audio_revision: int = 0, audio_sha256: str = "",
) -> dict:
    current = normalize_segments([dict(row) for row in segments])
    parsed = parse_change_request(comment)
    operations: list[dict] = []
    unresolved: list[dict] = []
    for instruction in parsed["instructions"]:
        kind = instruction.get("kind")
        if kind == "replace_text":
            targets = _text_targets(current, instruction)
            if targets:
                operations.extend(
                    _text_operation(
                        instruction=instruction, index=index, current=row,
                        proposed_text=proposed,
                    )
                    for index, row, proposed in targets
                )
            else:
                unresolved.append({
                    **_manual_operation(instruction),
                    "reason": "lyric_target_not_found",
                    "warnings": ["lyric_target_not_found"],
                })
        elif kind == "remove_terminal_period":
            matches = _period_operations(current, instruction)
            if matches:
                operations.extend(matches)
            else:
                unresolved.append({
                    **_manual_operation(instruction),
                    "reason": "no_terminal_period_found",
                    "warnings": ["no_terminal_period_found"],
                })
        else:
            unresolved.append(_manual_operation(instruction))

    applicable_count = len(operations)
    if applicable_count and unresolved:
        status = "partial"
    elif applicable_count:
        status = "ready"
    else:
        status = "needs_input"
    return {
        "schema_version": SCHEMA_VERSION,
        "parser_version": parsed["schema_version"],
        "status": status,
        "request_sha256": request_hash(comment),
        "base_revision": int(base_revision),
        "segments_hash": segments_hash(current),
        "segments_content_hash": segments_content_hash(current),
        "audio_revision": int(audio_revision or 0),
        "audio_sha256": str(audio_sha256 or ""),
        "operations": [*operations, *unresolved],
        "operation_count": len(operations) + len(unresolved),
        "applicable_count": applicable_count,
        "unresolved_count": len(unresolved),
    }


def apply_operations(
    current_segments: Iterable[dict], proposal: dict,
    operation_ids: Iterable[str],
) -> tuple[list[dict], list[dict]]:
    current = normalize_segments([dict(row) for row in current_segments])
    if segments_content_hash(current) != str(proposal.get("segments_content_hash") or ""):
        raise RuntimeError("change_request_proposal_stale")
    requested = [str(value) for value in operation_ids]
    if not requested or len(requested) != len(set(requested)):
        raise ValueError("operation_ids must be non-empty and unique")
    indexed = {
        str(row.get("id")): row for row in (proposal.get("operations") or [])
        if isinstance(row, dict)
    }
    selected = [indexed.get(value) for value in requested]
    if any(row is None for row in selected):
        raise ValueError("unknown change request operation")
    if any(not row.get("applicable") for row in selected):
        raise ValueError("manual-review operations cannot be applied")

    current_by_hash = {
        segments_content_hash([row]): row for row in current
    }
    claimed: set[str] = set()
    for operation in selected:
        before = operation.get("current_segments") or []
        after = operation.get("proposed_segments") or []
        if len(before) != 1 or len(after) != 1:
            raise ValueError("change request operations must replace one segment")
        before_hash = segments_content_hash(before)
        after_hash = segments_content_hash(after)
        if (
            before_hash != operation.get("current_segments_hash")
            or after_hash != operation.get("proposed_segments_hash")
            or before_hash not in current_by_hash
            or before_hash in claimed
        ):
            raise RuntimeError("change_request_proposal_stale")
        claimed.add(before_hash)

    result = [row for row in current if segments_content_hash([row]) not in claimed]
    for operation in selected:
        result.extend(deepcopy(operation["proposed_segments"]))
    result = normalize_segments(result)
    # Text-only operations must never move the timeline.
    before_timing = [(row["start"], row["end"]) for row in current]
    after_timing = [(row["start"], row["end"]) for row in result]
    if before_timing != after_timing:
        raise ValueError("text change request changed timeline")
    return result, [deepcopy(row) for row in selected]

