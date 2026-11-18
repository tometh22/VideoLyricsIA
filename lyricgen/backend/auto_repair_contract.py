"""Pure, fail-closed application of local reversible lyric candidates.

This module deliberately has no policy, environment, database, provider, or
editor dependencies.  It accepts only same-shape local edits: timing-only
changes that preserve all lyric tokens, or content-only changes that preserve
the line and word timing structure.  Candidate rows must exactly describe the
current source snapshot before they can be considered.
"""
from __future__ import annotations

from copy import deepcopy
import math
from typing import Any, Mapping, Sequence


_ID_KEYS = ("_id", "id", "segment_id")
_WINDOW_EPSILON = 1e-6
_SEVERE_OVERLAP_SECONDS = 0.50
_WORD_LINE_TOLERANCE_SECONDS = 0.05


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _stable_id(row: Mapping[str, Any]) -> tuple[str, str] | None:
    present = [(key, str(row[key]).strip()) for key in _ID_KEYS
               if row.get(key) is not None]
    if not present:
        return None
    if any(not value for _key, value in present):
        raise ValueError("unreconcilable_segment_id")
    if len({value for _key, value in present}) != 1:
        raise ValueError("conflicting_segment_ids")
    # Preserve which field supplied the identity: a candidate cannot silently
    # substitute one identifier namespace for another.
    return present[0]


def _line_interval(row: Mapping[str, Any]) -> tuple[float, float] | None:
    start, end = _finite_number(row.get("start")), _finite_number(row.get("end"))
    if start is None or end is None or start < 0 or end <= start:
        return None
    return start, end


def _words(row: Mapping[str, Any]) -> list[Mapping[str, Any]] | None:
    if "words" not in row:
        return None
    value = row.get("words")
    if not isinstance(value, (list, tuple)):
        return None
    if not all(isinstance(word, Mapping) for word in value):
        return None
    return list(value)


def _word_text(word: Mapping[str, Any]) -> tuple[str, str] | None:
    keys = [key for key in ("word", "text") if key in word]
    if len(keys) != 1 or not isinstance(word.get(keys[0]), str):
        return None
    value = str(word[keys[0]])
    if not value.strip():
        return None
    return keys[0], value


def _valid_words(row: Mapping[str, Any]) -> bool:
    words = _words(row)
    if words is None:
        return "words" not in row
    prior_start: float | None = None
    for word in words:
        if _word_text(word) is None:
            return False
        present = ("start" in word, "end" in word)
        if present[0] != present[1]:
            return False
        if not present[0]:
            continue
        start, end = _finite_number(word.get("start")), _finite_number(word.get("end"))
        if start is None or end is None or start < 0 or end < start:
            return False
        if prior_start is not None and start + _WINDOW_EPSILON < prior_start:
            return False
        prior_start = start
    return True


def _has_review_lock(row: Mapping[str, Any]) -> bool:
    return bool(row.get("locked")) or bool(row.get("review"))


def _row_ids(rows: Sequence[Mapping[str, Any]]) -> list[tuple[str, str] | None]:
    identities = [_stable_id(row) for row in rows]
    concrete = [identity for identity in identities if identity is not None]
    if concrete and len(concrete) != len(rows):
        raise ValueError("unreconcilable_segment_ids")
    if len(set(concrete)) != len(concrete):
        raise ValueError("duplicate_segment_id")
    return identities


def _reconcile_rows(
    source: Sequence[Mapping[str, Any]],
    current: Sequence[Mapping[str, Any]],
    proposed: Sequence[Mapping[str, Any]],
) -> list[int]:
    if not current or len(current) != len(proposed):
        raise ValueError("line_structure_changed")
    current_ids, proposed_ids = _row_ids(current), _row_ids(proposed)
    source_ids = _row_ids(source)
    if any(identity is not None for identity in current_ids + proposed_ids + source_ids):
        if not all(identity is not None for identity in current_ids + proposed_ids):
            raise ValueError("unreconcilable_segment_ids")
        if current_ids != proposed_ids:
            raise ValueError("line_order_or_ids_changed")
        source_index = {identity: index for index, identity in enumerate(source_ids)
                        if identity is not None}
        if len(source_index) != len(source_ids):
            raise ValueError("unreconcilable_source_ids")
        try:
            indexes = [source_index[identity] for identity in current_ids]
        except KeyError as exc:
            raise ValueError("segment_id_not_in_source") from exc
        if indexes != sorted(indexes) or len(set(indexes)) != len(indexes):
            raise ValueError("line_order_changed")
    else:
        # Without IDs, exact current rows must identify a unique, ordered span.
        indexes = []
        for row in current:
            matches = [index for index, original in enumerate(source)
                       if dict(original) == dict(row)]
            if len(matches) != 1:
                raise ValueError("current_rows_not_uniquely_reconcilable")
            indexes.append(matches[0])
        if indexes != sorted(indexes) or len(set(indexes)) != len(indexes):
            raise ValueError("line_order_changed")

    for index, row in zip(indexes, current):
        if dict(source[index]) != dict(row):
            raise ValueError("current_segments_mismatch")
    return indexes


def _mapping_except(row: Mapping[str, Any], excluded: set[str]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key not in excluded}


def _timing_pair(before: Mapping[str, Any], after: Mapping[str, Any]) -> tuple[bool, bool]:
    """Return (compatible, timing_changed) for a timing-only line delta."""
    if before.get("text") != after.get("text"):
        return False, False
    if _mapping_except(before, {"start", "end", "words"}) != _mapping_except(
        after, {"start", "end", "words"},
    ):
        return False, False
    before_words, after_words = _words(before), _words(after)
    if (before_words is None) != (after_words is None):
        return False, False
    changed = before.get("start") != after.get("start") or before.get("end") != after.get("end")
    if before_words is not None and after_words is not None:
        if len(before_words) != len(after_words):
            return False, False
        for left, right in zip(before_words, after_words):
            left_text, right_text = _word_text(left), _word_text(right)
            if left_text != right_text:
                return False, False
            if _mapping_except(left, {"start", "end"}) != _mapping_except(right, {"start", "end"}):
                return False, False
            changed = changed or left.get("start") != right.get("start") or left.get("end") != right.get("end")
    return True, changed


def _content_pair(before: Mapping[str, Any], after: Mapping[str, Any]) -> tuple[bool, bool]:
    """Return (compatible, content_changed) for a content-only line delta."""
    if before.get("start") != after.get("start") or before.get("end") != after.get("end"):
        return False, False
    if _mapping_except(before, {"text", "words"}) != _mapping_except(
        after, {"text", "words"},
    ):
        return False, False
    before_text, after_text = before.get("text"), after.get("text")
    if not isinstance(before_text, str) or not isinstance(after_text, str):
        return False, False
    # Keep line cardinality stable even when no word-level timestamps exist.
    if len(before_text.split()) != len(after_text.split()):
        return False, False
    if not after_text.strip():
        return False, False
    before_words, after_words = _words(before), _words(after)
    if (before_words is None) != (after_words is None):
        return False, False
    changed = before_text != after_text
    if before_words is not None and after_words is not None:
        if len(before_words) != len(after_words):
            return False, False
        for left, right in zip(before_words, after_words):
            left_text, right_text = _word_text(left), _word_text(right)
            if left_text is None or right_text is None:
                return False, False
            if left_text[0] != right_text[0]:
                return False, False
            if _mapping_except(left, {left_text[0]}) != _mapping_except(
                right, {right_text[0]},
            ):
                return False, False
            changed = changed or left_text[1] != right_text[1]
    return True, changed


def _declared_action(candidate: Mapping[str, Any]) -> str | None:
    value = candidate.get("action") or candidate.get("repair_action")
    if isinstance(value, str) and value in {"timing_reversible", "content_reversible"}:
        return str(value)
    if value is not None:
        return "unsupported_action"
    suggestion = candidate.get("suggestion_type")
    if suggestion == "timing":
        return "timing_reversible"
    if suggestion == "text":
        return "content_reversible"
    if suggestion is not None:
        return "unsupported_action"
    return None


def _candidate_identity(candidate: Mapping[str, Any], index: int) -> str:
    value = candidate.get("id") or candidate.get("window_id")
    return str(value)[:128] if value is not None else f"candidate-{index}"


def _diff_value(before: Any, after: Any, path: str = "") -> list[dict[str, Any]]:
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        changes = []
        for key in sorted(set(before) | set(after), key=str):
            child_path = f"{path}.{key}" if path else str(key)
            if key not in before:
                changes.append({"path": child_path, "before": None, "after": deepcopy(after[key])})
            elif key not in after:
                changes.append({"path": child_path, "before": deepcopy(before[key]), "after": None})
            else:
                changes.extend(_diff_value(before[key], after[key], child_path))
        return changes
    if isinstance(before, (list, tuple)) and isinstance(after, (list, tuple)):
        if len(before) != len(after):
            return [{"path": path, "before": deepcopy(before), "after": deepcopy(after)}]
        changes = []
        for index, (left, right) in enumerate(zip(before, after)):
            changes.extend(_diff_value(left, right, f"{path}[{index}]"))
        return changes
    if before != after:
        return [{"path": path, "before": deepcopy(before), "after": deepcopy(after)}]
    return []


def _valid_timeline(rows: Sequence[Mapping[str, Any]]) -> str | None:
    prior_start: float | None = None
    prior_end: float | None = None
    for row in rows:
        interval = _line_interval(row)
        if interval is None:
            return "invalid_interval"
        start, end = interval
        if prior_start is not None and start + _WINDOW_EPSILON < prior_start:
            return "timeline_inversion"
        if prior_end is not None and prior_end - start > _SEVERE_OVERLAP_SECONDS:
            return "severe_overlap"
        if not _valid_words(row):
            return "invalid_word_timing"
        prior_start, prior_end = start, end
    return None


def _window(candidate: Mapping[str, Any]) -> tuple[float, float]:
    start, end = _finite_number(candidate.get("start")), _finite_number(candidate.get("end"))
    if start is None or end is None or start < 0 or end <= start:
        raise ValueError("invalid_window")
    return start, end


def _window_contains_delta(
    before: Mapping[str, Any], after: Mapping[str, Any],
    start: float, end: float,
) -> bool:
    line_changed = before.get("text") != after.get("text") or before.get("start") != after.get("start") or before.get("end") != after.get("end") or before.get("words") != after.get("words")
    if not line_changed:
        return True
    old_interval, new_interval = _line_interval(before), _line_interval(after)
    if old_interval is None or new_interval is None:
        return False
    # The complete line delta must remain inside the candidate's local window;
    # do not let a contextual row drag neighboring lyrics along with it.
    if not (
        min(*old_interval, *new_interval) >= start - _WINDOW_EPSILON
        and max(*old_interval, *new_interval) <= end + _WINDOW_EPSILON
    ):
        return False
    before_words, after_words = _words(before), _words(after)
    if before_words is not None and after_words is not None:
        if len(before_words) != len(after_words):
            return False
        for left, right in zip(before_words, after_words):
            if left.get("start") == right.get("start") and left.get("end") == right.get("end"):
                continue
            for word, interval in ((left, old_interval), (right, new_interval)):
                word_start, word_end = _finite_number(word.get("start")), _finite_number(word.get("end"))
                if word_start is None or word_end is None:
                    return False
                if (
                    word_start < start - _WINDOW_EPSILON
                    or word_end > end + _WINDOW_EPSILON
                    or word_start < interval[0] - _WORD_LINE_TOLERANCE_SECONDS
                    or word_end > interval[1] + _WORD_LINE_TOLERANCE_SECONDS
                ):
                    return False
    return True


def apply_local_candidates(
    segments: Sequence[dict[str, Any]],
    quality_proposal_windows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Classify and apply safe local candidates on a copied segment snapshot.

    Each candidate is atomic.  Invalid candidates are reported as abstentions;
    unrelated, non-overlapping valid candidates may still be applied.  The
    returned ``diff`` contains only changed paths, and the caller's input is
    never mutated.
    """
    if not isinstance(segments, (list, tuple)) or not all(
        isinstance(row, Mapping) for row in segments
    ):
        return {
            "segments": deepcopy(segments), "diff": [], "applied": [],
            "abstentions": [{"candidate_id": None, "reason": "invalid_source_segments"}],
        }
    result = [deepcopy(dict(row)) for row in segments]
    abstentions: list[dict[str, str]] = []
    applied: list[dict[str, Any]] = []
    occupied_ids: set[tuple[str, str] | tuple[str, int]] = set()
    occupied_windows: list[tuple[float, float]] = []

    for candidate_index, raw in enumerate(quality_proposal_windows or []):
        cid = f"candidate-{candidate_index}"
        try:
            if not isinstance(raw, Mapping):
                raise ValueError("invalid_candidate")
            cid = _candidate_identity(raw, candidate_index)
            kind = raw.get("kind")
            if kind is not None and kind not in {
                "review_proposal_candidate", "review_proposal_window",
            }:
                raise ValueError("unsupported_candidate_kind")
            start, end = _window(raw)
            current_raw, proposed_raw = raw.get("current_segments"), raw.get("proposed_segments")
            if not isinstance(current_raw, (list, tuple)) or not isinstance(proposed_raw, (list, tuple)):
                raise ValueError("missing_candidate_segments")
            if not all(isinstance(row, Mapping) for row in (*current_raw, *proposed_raw)):
                raise ValueError("invalid_candidate_segments")
            current = [dict(row) for row in current_raw]
            proposed = [dict(row) for row in proposed_raw]
            indexes = _reconcile_rows(result, current, proposed)

            before_rows = [result[index] for index in indexes]
            if any(_has_review_lock(row) for row in before_rows):
                raise ValueError("locked_or_review_segment")
            if any(_has_review_lock(row) for row in proposed):
                raise ValueError("locked_or_review_candidate")
            for before, after in zip(before_rows, proposed):
                if _line_interval(before) is None or _line_interval(after) is None:
                    raise ValueError("invalid_interval")
                if not _valid_words(before) or not _valid_words(after):
                    raise ValueError("invalid_word_timing")
                if not _window_contains_delta(before, after, start, end):
                    raise ValueError("change_outside_window")

            timing_checks = [_timing_pair(before, after)
                             for before, after in zip(before_rows, proposed)]
            content_checks = [_content_pair(before, after)
                              for before, after in zip(before_rows, proposed)]
            timing_compatible = all(item[0] for item in timing_checks)
            content_compatible = all(item[0] for item in content_checks)
            timing_changed = any(item[1] for item in timing_checks)
            content_changed = any(item[1] for item in content_checks)
            if timing_compatible and timing_changed and not content_changed:
                action = "timing_reversible"
            elif content_compatible and content_changed and not timing_changed:
                action = "content_reversible"
            elif not timing_changed and not content_changed:
                raise ValueError("candidate_has_no_change")
            else:
                raise ValueError("unsupported_or_mixed_change")

            declared = _declared_action(raw)
            if declared == "unsupported_action":
                raise ValueError("unsupported_action")
            if declared is not None and declared != action:
                raise ValueError("declared_action_mismatch")

            row_keys: list[tuple[str, str] | tuple[str, int]] = []
            for index, row in zip(indexes, before_rows):
                identity = _stable_id(row)
                row_keys.append(identity if identity is not None else ("index", index))
            if any(key in occupied_ids for key in row_keys):
                raise ValueError("candidate_conflicts_with_applied_window")
            if any(start < other_end - _WINDOW_EPSILON and end > other_start + _WINDOW_EPSILON
                   for other_start, other_end in occupied_windows):
                raise ValueError("candidate_window_overlaps_applied_window")

            trial = [deepcopy(row) for row in result]
            for index, proposed_row in zip(indexes, proposed):
                trial[index] = deepcopy(proposed_row)
            # Preserve the original global ordering; timing edits that reorder
            # a line are rejected rather than silently sorting lyric rows.
            timeline_error = _valid_timeline(trial)
            if timeline_error:
                raise ValueError(timeline_error)

            candidate_diff = []
            for index, before, after in zip(indexes, before_rows, proposed):
                changes = _diff_value(before, after)
                if changes:
                    identity = _stable_id(before)
                    candidate_diff.append({
                        "segment_id": identity[1] if identity else None,
                        "segment_index": index,
                        "changes": changes,
                    })
            result = trial
            occupied_ids.update(row_keys)
            occupied_windows.append((start, end))
            applied.append({
                "candidate_id": cid, "action": action,
                "window": {"start": start, "end": end},
                "diff": candidate_diff,
            })
        except (TypeError, ValueError, OverflowError) as exc:
            abstentions.append({"candidate_id": cid, "reason": str(exc) or "invalid_candidate"})

    return {
        "segments": result,
        "diff": [item for candidate in applied for item in candidate["diff"]],
        "applied": applied,
        "abstentions": abstentions,
    }
