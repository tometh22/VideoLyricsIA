"""Review-only T4 proposals for word/line boundary defects.

The production payload historically uses one ``start``/``end`` pair for two
different clocks: the phonetic interval of the words and the display interval
of the lyric card.  This module separates those clocks in diagnostics and only
proposes a visible endpoint when the segment's *own* trusted word timestamps
prove that the current card cuts a word or hangs for an extreme amount of
time.

It deliberately has no audio or catalogue dependency and never mutates the
segments.  A proposal that would cross the next line needs an explicit,
hash-bound same-occurrence attestation; repeated lyrics may otherwise drag a
boundary to the wrong chorus occurrence.
"""
from __future__ import annotations

from copy import deepcopy
import math
import re
from typing import Any, Mapping, Sequence


_PROOF_SHA256 = re.compile(r"[0-9a-f]{64}")
_MIN_WORD_SCORE = 0.10
_MAX_WORD_DURATION_S = 8.0


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _trusted_word_clock(segment: Mapping[str, Any]) -> tuple[float, str] | None:
    """Return the last trustworthy word end and its immutable source."""

    provider = segment.get("provider_evidence")
    if isinstance(provider, Mapping) and isinstance(provider.get("words"), list):
        words = provider["words"]
        source = "provider_evidence_words"
    elif isinstance(segment.get("words"), list):
        words = segment["words"]
        source = "segment_words"
    else:
        return None

    ends: list[float] = []
    for word in words:
        if not isinstance(word, Mapping):
            continue
        start, end = _number(word.get("start")), _number(word.get("end"))
        if start is None or end is None or end < start:
            continue
        score = _number(word.get("score"))
        if score is not None and score < _MIN_WORD_SCORE:
            continue
        if end - start > _MAX_WORD_DURATION_S:
            continue
        ends.append(end)
    return (max(ends), source) if ends else None


def _occurrence_attestation(
    segment: Mapping[str, Any], index: int,
) -> dict[str, Any] | None:
    """Accept only the narrow, hash-bound adjacent occurrence contract."""

    for key in ("timing_occurrence_identity", "occurrence_identity"):
        raw = segment.get(key)
        if not isinstance(raw, Mapping):
            continue
        proof = str(raw.get("proof_sha256") or "").lower()
        try:
            indices_match = (
                int(raw.get("from_index", -1)) == index
                and int(raw.get("to_index", -1)) == index + 1
            )
        except (TypeError, ValueError):
            indices_match = False
        if (
            raw.get("same_occurrence") is True
            and indices_match
            and _PROOF_SHA256.fullmatch(proof)
        ):
            return {
                "schema_version": "t4-occurrence-attestation-v1",
                "same_occurrence": True,
                "from_index": index,
                "to_index": index + 1,
                "proof_sha256": proof,
            }
    return None


def _endpoint_attestation(
    segment: Mapping[str, Any], index: int, *,
    display_end: float, phonetic_end: float,
) -> dict[str, Any] | None:
    """Validate a hash-bound endpoint agreed by two timing families.

    This is intentionally an attestation contract, not an inference.  The
    quality worker can later populate it from independently computed acoustic
    and phonetic evidence; editable segment fields alone cannot create it.
    """

    raw = segment.get("timing_endpoint_attestation")
    if not isinstance(raw, Mapping):
        return None
    proof = str(raw.get("proof_sha256") or "").lower()
    candidate_end = _number(raw.get("candidate_end"))
    source_display_end = _number(raw.get("source_display_end"))
    source_phonetic_end = _number(raw.get("source_phonetic_end"))
    families = sorted({
        str(value).strip().lower()
        for value in (raw.get("families") or []) if str(value).strip()
    })
    try:
        index_matches = int(raw.get("segment_index", -1)) == index
    except (TypeError, ValueError):
        index_matches = False
    if not (
        index_matches
        and candidate_end is not None
        and source_display_end is not None
        and source_phonetic_end is not None
        and abs(source_display_end - display_end) <= 0.001
        and abs(source_phonetic_end - phonetic_end) <= 0.001
        and len(families) >= 2
        and _PROOF_SHA256.fullmatch(proof)
    ):
        return None
    return {
        "schema_version": "t4-independent-endpoint-attestation-v1",
        "segment_index": index,
        "candidate_end": candidate_end,
        "source_display_end": source_display_end,
        "source_phonetic_end": source_phonetic_end,
        "families": families,
        "proof_sha256": proof,
    }


def build_structural_t4_shadow(
    segments: Sequence[dict[str, Any]],
    *,
    boundary_tolerance_s: float = 0.06,
    minimum_visible_change_s: float = 0.15,
    extreme_overhang_s: float = 2.0,
) -> dict[str, Any]:
    """Build occurrence-safe structural timing proposals without mutation.

    The returned ``proposals`` are review candidates, never automatic edits.
    Fixed 250 ms padding is diagnosed but not removed: the human endpoint set
    showed that a global padding rewrite damages valid display timing.
    """

    if boundary_tolerance_s <= 0 or minimum_visible_change_s <= 0:
        raise ValueError("timing tolerances must be positive")
    frozen = deepcopy(list(segments))
    rows: list[dict[str, Any]] = []
    proposals: list[dict[str, Any]] = []
    reason_counts: dict[str, int] = {}

    def count(reason: str) -> None:
        reason_counts[reason] = reason_counts.get(reason, 0) + 1

    for index, segment in enumerate(frozen):
        if not isinstance(segment, Mapping):
            count("invalid_segment")
            continue
        start = _number(segment.get("display_start", segment.get("start")))
        display_end = _number(segment.get("display_end", segment.get("end")))
        next_start = None
        if index + 1 < len(frozen) and isinstance(frozen[index + 1], Mapping):
            next_start = _number(
                frozen[index + 1].get(
                    "display_start", frozen[index + 1].get("start"),
                )
            )
        clock = _trusted_word_clock(segment)
        phonetic_end, clock_source = clock if clock else (None, None)
        at_next_start = bool(
            display_end is not None and next_start is not None
            and abs(display_end - next_start) <= boundary_tolerance_s
        )
        word_at_next_start = bool(
            phonetic_end is not None and next_start is not None
            and abs(phonetic_end - next_start) <= boundary_tolerance_s
        )
        padding = (
            display_end - phonetic_end
            if display_end is not None and phonetic_end is not None else None
        )
        fixed_padding = bool(
            padding is not None and abs(padding - 0.25) <= 0.075
        )
        occurrence = _occurrence_attestation(segment, index)
        endpoint = (
            _endpoint_attestation(
                segment, index, display_end=display_end,
                phonetic_end=phonetic_end,
            )
            if display_end is not None and phonetic_end is not None else None
        )
        action = "abstain"
        reason = "missing_trusted_word_clock"
        proposed_end = None

        if at_next_start and word_at_next_start:
            diagnosis = "upstream_shared_word_line_boundary"
        elif display_end is not None and phonetic_end is not None and (
            display_end < phonetic_end - minimum_visible_change_s
        ):
            diagnosis = "card_ends_before_trusted_last_word"
        elif fixed_padding:
            diagnosis = "fixed_wrapper_padding"
        elif at_next_start:
            diagnosis = "display_boundary_inherited_from_next_line"
        else:
            diagnosis = "independent_or_insufficient_evidence"

        if segment.get("locked") is True:
            reason = "operator_locked"
        elif start is None or display_end is None or display_end <= start:
            reason = "invalid_display_clock"
        elif phonetic_end is None:
            reason = "missing_trusted_word_clock"
        elif endpoint is not None:
            proposed_end = float(endpoint["candidate_end"])
            crosses_next = bool(
                next_start is not None
                and proposed_end > next_start + boundary_tolerance_s
            )
            if crosses_next and occurrence is None:
                reason = "occurrence_identity_required"
                proposed_end = None
            elif abs(proposed_end - display_end) < minimum_visible_change_s:
                reason = "independent_endpoint_confirms_current_boundary"
                proposed_end = None
            else:
                action = (
                    "extend_display_to_independent_endpoint"
                    if proposed_end > display_end
                    else "trim_display_to_independent_endpoint"
                )
                reason = "independent_endpoint_consensus"
        elif display_end < phonetic_end - minimum_visible_change_s:
            # A single ASR family's word clock is a symptom, not a judge.  The
            # first replay produced 14 collateral regressions when it was used
            # directly, so visible proposals now require the contract above.
            reason = "independent_endpoint_attestation_required"
        elif display_end > phonetic_end + extreme_overhang_s and at_next_start:
            reason = "independent_endpoint_attestation_required"
        elif fixed_padding:
            reason = "fixed_padding_requires_display_calibration"
        elif at_next_start:
            reason = "display_boundary_inherited_from_next_line"
        else:
            reason = "no_structural_defect_requiring_visible_change"

        if proposed_end is not None and proposed_end <= start:
            action, reason, proposed_end = (
                "abstain", "candidate_would_be_non_positive", None,
            )

        row = {
            "segment_index": index,
            "display_start": start,
            "display_end": display_end,
            "phonetic_end": phonetic_end,
            "phonetic_clock_source": clock_source,
            "next_line_start": next_start,
            "display_end_at_next_start": at_next_start,
            "phonetic_end_at_next_start": word_at_next_start,
            "fixed_250ms_padding": fixed_padding,
            "diagnosis": diagnosis,
            "action": action,
            "reason": reason,
            "candidate_display_end": (
                round(proposed_end, 6) if proposed_end is not None else None
            ),
            "visible_delta_s": (
                round(proposed_end - display_end, 6)
                if proposed_end is not None and display_end is not None else None
            ),
            "occurrence_identity_attested": occurrence is not None,
            "occurrence_attestation": occurrence,
            "independent_endpoint_attested": endpoint is not None,
            "endpoint_attestation": endpoint,
            "automatic_timing_change_allowed": False,
            "mutated_segment": False,
        }
        rows.append(row)
        count(reason)
        if proposed_end is not None:
            proposals.append(row)

    return {
        "schema_version": "structural-t4-shadow-v1",
        "mode": "observe",
        "segment_count": len(frozen),
        "proposal_count": len(proposals),
        "abstention_count": len(frozen) - len(proposals),
        "reason_counts": reason_counts,
        "proposals": proposals,
        "rows": rows,
        "mutated_segments": False,
        "automatic_timing_change_allowed": False,
        "cross_occurrence_requires_attestation": True,
        "visible_change_requires_independent_endpoint_attestation": True,
        "reference_data_used": False,
    }
