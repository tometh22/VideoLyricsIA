"""Pure builders for recoverable editor-training evidence.

Raw lyrics stay in the tenant-private editor snapshots.  Audit rows contain
exact timings and keyed text references, which is enough to measure operator
actions without turning ``audit_log`` into another lyric store.
"""
from __future__ import annotations

import math
from collections import defaultdict
from difflib import SequenceMatcher
from typing import Any, Callable, Iterable

from machine_evidence import (
    MachineSnapshotMissing,
    SCHEMA as MACHINE_EVIDENCE_SCHEMA,
    snapshot_hash,
    validate_machine_evidence,
    validate_quality_training_signal,
)


LINE_DELTA_SCHEMA = "editor-line-delta-v2"
TRAINING_PAIR_SCHEMA = "transcription-training-pair-v1"
TIMING_NOISE_THRESHOLD_S = 0.05
MAX_ALIGNMENT_CELLS = 250_000
ALIGNMENT_LOOKAHEAD = 32
MAX_BANDED_ALIGNMENT_CELLS = 1_000_000


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


def _row_match_cost(before: dict, after: dict) -> float:
    """Cost for monotonic legacy-row alignment; two gaps cost exactly 2."""
    before_id = str(before.get("_id") or "")
    after_id = str(after.get("_id") or "")
    if before_id and after_id and before_id != after_id:
        return math.inf
    left, right = _text(before.get("text")).casefold(), _text(after.get("text")).casefold()
    similarity = 1.0 if left == right else SequenceMatcher(None, left, right).ratio()
    timing_distance = (
        abs(_finite_time(before.get("start")) - _finite_time(after.get("start")))
        + abs(_finite_time(before.get("end")) - _finite_time(after.get("end")))
    )
    # Text carries most of the identity; timing breaks ties without preventing
    # a legitimate line from moving after an insertion.
    return min(1.999, (1.0 - similarity) * 1.5 + min(timing_distance / 2.0, 1.0) * 0.5)


def _align_unmatched_rows(
    before: list[dict], after: list[dict], before_indices: list[int], after_indices: list[int],
) -> tuple[list[tuple[int, int]], bool]:
    """Needleman-Wunsch alignment for rows that lack a common stable ID."""
    rows, cols = len(before_indices), len(after_indices)
    if rows * cols > MAX_ALIGNMENT_CELLS:
        return _align_unmatched_rows_bounded(
            before, after, before_indices, after_indices,
        )
    gap_cost = 1.0
    costs = [[math.inf] * (cols + 1) for _ in range(rows + 1)]
    steps: list[list[str | None]] = [[None] * (cols + 1) for _ in range(rows + 1)]
    costs[0][0] = 0.0
    for left in range(1, rows + 1):
        costs[left][0] = left * gap_cost
        steps[left][0] = "delete"
    for right in range(1, cols + 1):
        costs[0][right] = right * gap_cost
        steps[0][right] = "insert"
    for left in range(1, rows + 1):
        for right in range(1, cols + 1):
            match_cost = _row_match_cost(
                before[before_indices[left - 1]], after[after_indices[right - 1]],
            )
            candidates = [
                (costs[left - 1][right - 1] + match_cost, 0, "match"),
                (costs[left - 1][right] + gap_cost, 1, "delete"),
                (costs[left][right - 1] + gap_cost, 2, "insert"),
            ]
            best_cost, _priority, step = min(candidates)
            costs[left][right], steps[left][right] = best_cost, step
    aligned: list[tuple[int, int]] = []
    left, right = rows, cols
    while left or right:
        step = steps[left][right]
        if step == "match":
            aligned.append((before_indices[left - 1], after_indices[right - 1]))
            left -= 1
            right -= 1
        elif step == "delete":
            left -= 1
        elif step == "insert":
            right -= 1
        else:  # defensive: only reachable for a malformed DP matrix
            break
    return list(reversed(aligned)), True


def _align_unmatched_rows_bounded(
    before: list[dict], after: list[dict], before_indices: list[int], after_indices: list[int],
) -> tuple[list[tuple[int, int]], bool]:
    """Banded Needleman-Wunsch fallback for unusually large edits.

    The previous greedy lookahead could not represent two consecutive gaps:
    because a substitution costs less than two gaps, it paired an old row with
    the first newly inserted row and shifted every later label.  This keeps the
    exact dynamic-programming recurrence, but evaluates only a corridor around
    the proportional input/output diagonal.  Consecutive insertions and
    deletions are first-class paths without restoring the quadratic matrix.
    """
    rows, cols = len(before_indices), len(after_indices)
    if not rows or not cols:
        return [], True

    gap_cost = 1.0
    # Widen just enough for very unbalanced inputs to keep adjacent corridor
    # rows connected, while retaining O((rows + cols) * lookahead) behaviour.
    imbalance_step = math.ceil(abs(cols - rows) / max(1, min(rows, cols)))
    initial_band = min(
        max(rows, cols), ALIGNMENT_LOOKAHEAD + imbalance_step,
    )

    # Probe a bounded, evenly spaced sample for unambiguous identity anchors.
    # This discovers a localized shift even when the best path inside the
    # initial corridor is a plausible-looking (but wrong) substitution path.
    probe_count = min(rows, 64)
    probe_positions = sorted({
        round(index * (rows - 1) / max(1, probe_count - 1))
        for index in range(probe_count)
    })
    required_band = initial_band
    confident_anchors = 0
    for left in probe_positions:
        best_cost = second_cost = math.inf
        best_right = -1
        for right in range(cols):
            candidate = _row_match_cost(
                before[before_indices[left]], after[after_indices[right]],
            )
            if candidate < best_cost:
                second_cost = best_cost
                best_cost, best_right = candidate, right
            elif candidate < second_cost:
                second_cost = candidate
        if best_right < 0:
            continue
        if best_cost <= 0.75 and second_cost - best_cost >= 0.15:
            confident_anchors += 1
            center = round((left + 1) * cols / rows)
            required_band = min(
                max(rows, cols),
                max(required_band, abs((best_right + 1) - center) + 4),
            )

    budget_band = min(
        max(rows, cols),
        max(0, (MAX_BANDED_ALIGNMENT_CELLS // (rows + 1) - 1) // 2),
    )
    if (
        required_band > budget_band
        or (max(rows, cols) > ALIGNMENT_LOOKAHEAD and confident_anchors == 0)
    ):
        return [], False

    def run(band: int) -> tuple[list[tuple[int, int]], bool]:
        def bounds(left: int) -> tuple[int, int]:
            center = round(left * cols / rows)
            return (
                max(0, center - band),
                min(cols, center + band),
            )

        # Only two score rows are retained. Traceback remains bounded by the
        # corridor, and is discarded/retried if the best path hits its edge.
        _first_low, first_high = bounds(0)
        previous = {right: right * gap_cost for right in range(first_high + 1)}
        steps: dict[tuple[int, int], str] = {
            (0, right): "insert" for right in range(1, first_high + 1)
        }
        row_bounds: list[tuple[int, int]] = [(0, first_high)]

        for left in range(1, rows + 1):
            low, high = bounds(left)
            row_bounds.append((low, high))
            current: dict[int, float] = {}
            for right in range(low, high + 1):
                candidates: list[tuple[float, int, str]] = []
                if right > 0 and right - 1 in previous:
                    match_cost = _row_match_cost(
                        before[before_indices[left - 1]],
                        after[after_indices[right - 1]],
                    )
                    if math.isfinite(match_cost):
                        candidates.append((previous[right - 1] + match_cost, 0, "match"))
                if right in previous:
                    candidates.append((previous[right] + gap_cost, 1, "delete"))
                if right > 0 and right - 1 in current:
                    candidates.append((current[right - 1] + gap_cost, 2, "insert"))
                if not candidates:
                    continue
                best_cost, _priority, step = min(candidates)
                current[right] = best_cost
                steps[(left, right)] = step
            previous = current

        if cols not in previous:
            return [], True
        aligned: list[tuple[int, int]] = []
        touched_artificial_edge = False
        left, right = rows, cols
        while left or right:
            low, high = row_bounds[left]
            if (low > 0 and right == low) or (high < cols and right == high):
                touched_artificial_edge = True
            step = steps.get((left, right))
            if step == "match":
                aligned.append((before_indices[left - 1], after_indices[right - 1]))
                left -= 1
                right -= 1
            elif step == "delete":
                left -= 1
            elif step == "insert":
                right -= 1
            else:
                return [], True
        return list(reversed(aligned)), touched_artificial_edge

    band = required_band
    while True:
        aligned, touched_edge = run(band)
        if not touched_edge:
            return aligned, True
        wider_band = min(budget_band, band * 2)
        if wider_band == band:
            # Never manufacture training identity when the safe path falls
            # outside the resource-bounded corridor.
            return aligned, False
        band = wider_band


def _match_rows(
    before: list[dict], after: list[dict],
) -> tuple[list[tuple], list[int], list[int], bool]:
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
        after_id = str(row.get("_id") or "")
        before_index = next((
            index for index in candidates
            if index not in used_before
            and not (
                str(before[index].get("_id") or "")
                and after_id
                and str(before[index].get("_id")) != after_id
            )
        ), None)
        if before_index is None:
            continue
        candidates.remove(before_index)
        before_id = str(before[before_index].get("_id") or "")
        matched.append((
            before_index, after_index,
            after_id or before_id or f"idx_{before_index}",
        ))
        used_before.add(before_index)
        used_after.add(after_index)

    # Initial machine rows frequently acquire frontend IDs on first save. A
    # positional zip corrupts labels when that same save inserts a new line,
    # so align the unmatched sequences and let gaps represent insert/delete.
    before_remaining = [i for i in range(len(before)) if i not in used_before]
    after_remaining = [i for i in range(len(after)) if i not in used_after]
    aligned_rows, alignment_complete = _align_unmatched_rows(
        before, after, before_remaining, after_remaining,
    )
    for before_index, after_index in aligned_rows:
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
        alignment_complete,
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
    matched, removed, inserted, alignment_complete = _match_rows(before, after)
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
            "reorders": sum(
                bool(row["fields"]["order"])
                for row in changes if row["operation"] == "update"
            ),
        },
        "timing_noise_threshold_ms": round(timing_noise_threshold_s * 1000, 3),
        "truncated": False,
        "alignment_complete": alignment_complete,
        "contains_raw_lyrics": False,
    }


def _approved_version(versions: Iterable[Any]) -> Any | None:
    approved = [row for row in versions if bool(getattr(row, "is_approved", False))]
    return max(approved, key=lambda row: int(getattr(row, "revision", 0))) if approved else None


def _delta_content_projection(detail: dict) -> list[dict]:
    """Canonical, privacy-safe delta content used to verify the audit chain."""
    projected = []
    for raw_change in detail.get("changes") or []:
        change = dict(raw_change or {})

        def line(value: Any) -> dict | None:
            if not isinstance(value, dict):
                return None
            return {
                "start": _finite_time(value.get("start")),
                "end": _finite_time(value.get("end")),
                "text_length": int(value.get("text_length") or 0),
            }

        projected.append({
            "operation": change.get("operation"),
            "line_id": change.get("line_id"),
            "from_index": change.get("from_index"),
            "to_index": change.get("to_index"),
            "before": line(change.get("before")),
            "after": line(change.get("after")),
            "fields": dict(change.get("fields") or {}),
            "start_delta_ms": change.get("start_delta_ms"),
            "end_delta_ms": change.get("end_delta_ms"),
        })
    return projected


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
    elif approved:
        approved_segments = list(getattr(approved, "segments", None) or [])
        if approval_evidence.get("revision") != int(getattr(approved, "revision", -1)):
            issues.append("approval_revision_mismatch")
        if approval_evidence.get("segments_sha256") != snapshot_hash(approved_segments):
            issues.append("approved_snapshot_hash_mismatch")
        expected_approval_hash = snapshot_hash({
            key: value for key, value in approval_evidence.items()
            if key != "evidence_sha256"
        })
        if approval_evidence.get("evidence_sha256") != expected_approval_hash:
            issues.append("approval_evidence_hash_mismatch")
        try:
            validate_quality_training_signal(
                approval_evidence.get("song_quality_signal")
            )
        except MachineSnapshotMissing as exc:
            issues.append(str(exc))

    approved_revision = int(getattr(approved, "revision", -1)) if approved else -1
    delta_events = []
    legacy_or_truncated = False
    for audit in audits:
        detail = dict(getattr(audit, "detail", None) or {})
        try:
            to_revision = int(detail.get("to_revision"))
        except (TypeError, ValueError):
            to_revision = None
        # The selected approval is the label boundary. Later drafts belong to
        # a future training pair and must never contaminate this trajectory.
        if approved is not None and to_revision is not None and to_revision > approved_revision:
            continue
        if detail.get("schema") != LINE_DELTA_SCHEMA or detail.get("truncated") is not False:
            legacy_or_truncated = True
            continue
        if detail.get("alignment_complete") is not True:
            issues.append(
                "editor_delta_alignment_ambiguous:"
                f"{detail.get('from_revision')}->{detail.get('to_revision')}"
            )
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

    checkpoint_rows = [
        row for row in versions
        if approved is None or int(getattr(row, "revision", 0)) <= approved_revision
    ]
    if not checkpoint_rows or str(getattr(checkpoint_rows[0], "reason", "")) != "transcription":
        issues.append("transcription_checkpoint_missing")
    elif snapshot_hash(
        list(getattr(checkpoint_rows[0], "segments", None) or [])
    ) != snapshot_hash(original):
        issues.append("transcription_checkpoint_snapshot_mismatch")
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
        for row in checkpoint_rows
    ]
    deltas_by_boundary: dict[tuple[int, int], list[dict]] = defaultdict(list)
    for event in delta_events:
        try:
            boundary = (int(event.get("from_revision")), int(event.get("to_revision")))
        except (TypeError, ValueError):
            issues.append("editor_delta_revision_invalid")
            continue
        deltas_by_boundary[boundary].append(event)
    adjacent_boundaries = {
        (
            int(getattr(previous, "revision", 0)),
            int(getattr(current, "revision", 0)),
        )
        for previous, current in zip(checkpoint_rows, checkpoint_rows[1:])
    }
    for boundary in deltas_by_boundary:
        if boundary not in adjacent_boundaries:
            issues.append(f"editor_delta_orphaned:{boundary[0]}->{boundary[1]}")
    for previous, current in zip(checkpoint_rows, checkpoint_rows[1:]):
        previous_segments = list(getattr(previous, "segments", None) or [])
        current_segments = list(getattr(current, "segments", None) or [])
        material = build_line_delta_audit(
            previous_segments,
            current_segments,
            job_id=str(getattr(job, "job_id", "")),
            from_revision=int(getattr(previous, "revision", 0)),
            to_revision=int(getattr(current, "revision", 0)),
            checkpoint="export_validation",
            text_ref=lambda _value: None,
        )
        if material is None:
            continue
        boundary = (
            int(getattr(previous, "revision", 0)),
            int(getattr(current, "revision", 0)),
        )
        matches = deltas_by_boundary.get(boundary, [])
        if not matches:
            issues.append(f"editor_delta_missing:{boundary[0]}->{boundary[1]}")
        elif len(matches) > 1:
            issues.append(f"editor_delta_ambiguous:{boundary[0]}->{boundary[1]}")
        elif _delta_content_projection(matches[0]) != _delta_content_projection(material):
            issues.append(
                f"editor_delta_content_mismatch:{boundary[0]}->{boundary[1]}"
            )
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
