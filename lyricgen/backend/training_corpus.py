"""Pure builders for recoverable editor-training evidence.

Raw lyrics stay in the tenant-private editor snapshots.  Audit rows contain
exact timings and keyed text references, which is enough to measure operator
actions without turning ``audit_log`` into another lyric store.
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Callable, Iterable

from machine_evidence import (
    SCHEMA as MACHINE_EVIDENCE_SCHEMA,
    snapshot_hash,
    validate_machine_evidence,
)


LINE_DELTA_SCHEMA = "editor-line-delta-v2"
TRAINING_PAIR_SCHEMA = "transcription-training-pair-v1"
TIMING_NOISE_THRESHOLD_S = 0.05


def _finite_time(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed if math.isfinite(parsed) else 0.0


def _text(value: Any) -> str:
    return str(value or "").strip()


def _signature(row: dict) -> tuple:
    return (
        round(_finite_time(row.get("start")), 4),
        round(_finite_time(row.get("end")), 4),
        _text(row.get("text")),
    )


def _match_rows(before: list[dict], after: list[dict]) -> tuple[list[tuple], list[int], list[int]]:
    """Match stable IDs first, then exact content, then positional legacy rows."""
    matched: list[tuple[int, int, str]] = []
    used_before: set[int] = set()
    used_after: set[int] = set()
    before_ids = {
        str(row.get("_id")): index for index, row in enumerate(before)
        if row.get("_id") not in (None, "")
    }
    for after_index, row in enumerate(after):
        row_id = str(row.get("_id") or "")
        before_index = before_ids.get(row_id) if row_id else None
        if before_index is None or before_index in used_before:
            continue
        matched.append((before_index, after_index, row_id))
        used_before.add(before_index)
        used_after.add(after_index)

    by_signature: dict[tuple, list[int]] = defaultdict(list)
    for index, row in enumerate(before):
        if index not in used_before:
            by_signature[_signature(row)].append(index)
    for after_index, row in enumerate(after):
        if after_index in used_after:
            continue
        candidates = by_signature.get(_signature(row)) or []
        while candidates and candidates[0] in used_before:
            candidates.pop(0)
        if not candidates:
            continue
        before_index = candidates.pop(0)
        matched.append((before_index, after_index, f"idx_{before_index}"))
        used_before.add(before_index)
        used_after.add(after_index)

    # Initial machine rows frequently acquire frontend IDs on first save.
    # Pair remaining rows by position only when at least one side has no ID;
    # never override two conflicting stable IDs.
    for before_index, after_index in zip(
        [i for i in range(len(before)) if i not in used_before],
        [i for i in range(len(after)) if i not in used_after],
    ):
        before_id = str(before[before_index].get("_id") or "")
        after_id = str(after[after_index].get("_id") or "")
        if before_id and after_id and before_id != after_id:
            continue
        row_id = after_id or before_id or f"idx_{before_index}"
        matched.append((before_index, after_index, row_id))
        used_before.add(before_index)
        used_after.add(after_index)
    return (
        sorted(matched),
        [i for i in range(len(before)) if i not in used_before],
        [i for i in range(len(after)) if i not in used_after],
    )


def _line_view(row: dict, text_ref: Callable[[str], str | None]) -> dict:
    lexical = _text(row.get("text"))
    return {
        "start": _finite_time(row.get("start")),
        "end": _finite_time(row.get("end")),
        "text_length": len(lexical),
        "text_hmac": text_ref(lexical),
    }


def build_line_delta_audit(
    before_value: Any,
    after_value: Any,
    *,
    job_id: str,
    from_revision: int,
    to_revision: int,
    checkpoint: str,
    text_ref: Callable[[str], str | None],
    timing_noise_threshold_s: float = TIMING_NOISE_THRESHOLD_S,
) -> dict | None:
    """Return a complete, untruncated material delta or ``None`` for UI noise."""
    before = [dict(row) for row in (before_value or []) if isinstance(row, dict)]
    after = [dict(row) for row in (after_value or []) if isinstance(row, dict)]
    matched, removed, inserted = _match_rows(before, after)
    changes: list[dict] = []
    before_rank = {
        (left, right): rank
        for rank, (left, right, _row_id) in enumerate(
            sorted(matched, key=lambda item: item[0])
        )
    }
    after_rank = {
        (left, right): rank
        for rank, (left, right, _row_id) in enumerate(
            sorted(matched, key=lambda item: item[1])
        )
    }

    for before_index, after_index, row_id in matched:
        old, new = before[before_index], after[after_index]
        old_start, new_start = _finite_time(old.get("start")), _finite_time(new.get("start"))
        old_end, new_end = _finite_time(old.get("end")), _finite_time(new.get("end"))
        start_delta = new_start - old_start
        end_delta = new_end - old_end
        start_changed = abs(start_delta) >= timing_noise_threshold_s
        end_changed = abs(end_delta) >= timing_noise_threshold_s
        text_changed = _text(old.get("text")) != _text(new.get("text"))
        # Absolute indices shift after a normal insertion/deletion.  Only a
        # change in the relative order of surviving lines is a reorder label.
        order_changed = before_rank[(before_index, after_index)] != after_rank[
            (before_index, after_index)
        ]
        if not (start_changed or end_changed or text_changed or order_changed):
            continue
        changes.append({
            "operation": "update",
            "line_id": row_id,
            "from_index": before_index,
            "to_index": after_index,
            "before": _line_view(old, text_ref),
            "after": _line_view(new, text_ref),
            "fields": {
                "text": text_changed,
                "start": start_changed,
                "end": end_changed,
                "order": order_changed,
            },
            # Jitter below the threshold is deliberately zeroed here even
            # though exact endpoints remain available in before/after.
            "start_delta_ms": round(start_delta * 1000, 3) if start_changed else 0.0,
            "end_delta_ms": round(end_delta * 1000, 3) if end_changed else 0.0,
        })

    for before_index in removed:
        row = before[before_index]
        changes.append({
            "operation": "delete",
            "line_id": str(row.get("_id") or f"idx_{before_index}"),
            "from_index": before_index,
            "to_index": None,
            "before": _line_view(row, text_ref),
            "after": None,
            "fields": {"text": True, "start": True, "end": True, "order": True},
            "start_delta_ms": None,
            "end_delta_ms": None,
        })
    for after_index in inserted:
        row = after[after_index]
        changes.append({
            "operation": "insert",
            "line_id": str(row.get("_id") or f"idx_{after_index}"),
            "from_index": None,
            "to_index": after_index,
            "before": None,
            "after": _line_view(row, text_ref),
            "fields": {"text": True, "start": True, "end": True, "order": True},
            "start_delta_ms": None,
            "end_delta_ms": None,
        })
    changes.sort(key=lambda row: (
        row["to_index"] if row["to_index"] is not None else 10**9,
        row["from_index"] if row["from_index"] is not None else 10**9,
        row["line_id"],
    ))
    if not changes:
        return None
    return {
        "schema": LINE_DELTA_SCHEMA,
        "job_id": str(job_id),
        "from_revision": int(from_revision),
        "to_revision": int(to_revision),
        "checkpoint": str(checkpoint or "unknown")[:32],
        "before_line_count": len(before),
        "after_line_count": len(after),
        "changes": changes,
        "summary": {
            "changed_lines": len(changes),
            "text_changes": sum(bool(row["fields"]["text"]) for row in changes),
            "timing_changes": sum(
                bool(row["fields"]["start"] or row["fields"]["end"])
                for row in changes if row["operation"] == "update"
            ),
            "insertions": sum(row["operation"] == "insert" for row in changes),
            "deletions": sum(row["operation"] == "delete" for row in changes),
            "reorders": sum(bool(row["fields"]["order"]) for row in changes),
        },
        "timing_noise_threshold_ms": round(timing_noise_threshold_s * 1000, 3),
        "truncated": False,
        "contains_raw_lyrics": False,
    }


def _approved_version(versions: Iterable[Any]) -> Any | None:
    approved = [row for row in versions if bool(getattr(row, "is_approved", False))]
    return max(approved, key=lambda row: int(getattr(row, "revision", 0))) if approved else None


def materialize_training_pair(
    *, job: Any, document: Any, versions: Iterable[Any], audits: Iterable[Any],
) -> dict:
    """Build one export row and fail visibly when any invariant is missing."""
    versions = sorted(versions, key=lambda row: int(getattr(row, "revision", 0)))
    approved = _approved_version(versions)
    issues: list[str] = []
    evidence = dict(getattr(document, "machine_evidence", None) or {}) if document else {}
    original = list(getattr(document, "original_segments", None) or []) if document else []
    try:
        validate_machine_evidence(evidence, original)
    except Exception as exc:  # exporter reports evidence defects; it never repairs them
        issues.append(str(exc))
    if evidence.get("schema") != MACHINE_EVIDENCE_SCHEMA:
        issues.append("machine_evidence_not_current_schema")
    if approved is None:
        issues.append("approved_editor_version_missing")
    approval_provenance = dict(getattr(approved, "provenance", None) or {}) if approved else {}
    approval_evidence = dict(approval_provenance.get("training_approval") or {})
    if approval_evidence.get("schema") != "training-approval-evidence-v1":
        issues.append("approval_training_signal_missing")
    elif approved and approval_evidence.get("segments_sha256") != snapshot_hash(
        list(getattr(approved, "segments", None) or [])
    ):
        issues.append("approved_snapshot_hash_mismatch")

    delta_events = []
    legacy_or_truncated = False
    for audit in audits:
        detail = dict(getattr(audit, "detail", None) or {})
        if detail.get("schema") != LINE_DELTA_SCHEMA or detail.get("truncated") is not False:
            legacy_or_truncated = True
            continue
        delta_events.append({
            "audit_id": getattr(audit, "id", None),
            "created_at": (
                getattr(audit, "created_at", None).isoformat()
                if getattr(audit, "created_at", None) else None
            ),
            **detail,
        })
    if legacy_or_truncated:
        issues.append("legacy_or_truncated_editor_delta")
    delta_events.sort(key=lambda row: (
        int(row.get("to_revision") or 0), int(row.get("audit_id") or 0),
    ))

    approved_revision = int(getattr(approved, "revision", -1)) if approved else -1
    checkpoints = [
        {
            "version_id": str(getattr(row, "id", "")),
            "revision": int(getattr(row, "revision", 0)),
            "reason": str(getattr(row, "reason", "unknown")),
            "created_at": (
                getattr(row, "created_at", None).isoformat()
                if getattr(row, "created_at", None) else None
            ),
            "segments": list(getattr(row, "segments", None) or []),
            "segments_sha256": snapshot_hash(list(getattr(row, "segments", None) or [])),
        }
        for row in versions
        if approved is None or int(getattr(row, "revision", 0)) <= approved_revision
    ]
    return {
        "schema": TRAINING_PAIR_SCHEMA,
        "job_id": str(getattr(job, "job_id", "")),
        "tenant_id": str(getattr(job, "tenant_id", "")),
        "metadata": {
            "artist": str(getattr(job, "artist", "") or ""),
            "song_title": str(getattr(job, "song_title", "") or ""),
        },
        "complete": not issues,
        "issues": sorted(set(issues)),
        "pre_human": {
            "segments": original,
            "segments_sha256": snapshot_hash(original),
            "audio_sha256": (evidence.get("pre_human") or {}).get("audio_sha256"),
            "audio_revision": (evidence.get("pre_human") or {}).get("audio_revision"),
        },
        "hypotheses_by_family": list(evidence.get("hypotheses_by_family") or []),
        "machine_decisions": dict(evidence.get("decisions") or {}),
        "approved": ({
            "version_id": str(getattr(approved, "id", "")),
            "revision": approved_revision,
            "segments": list(getattr(approved, "segments", None) or []),
            "segments_sha256": snapshot_hash(list(getattr(approved, "segments", None) or [])),
            "training_approval": approval_evidence,
        } if approved else None),
        "intermediate_checkpoints": checkpoints,
        "intermediate_line_deltas": delta_events,
    }
