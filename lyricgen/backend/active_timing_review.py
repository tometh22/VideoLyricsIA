"""Directed acoustic investigation, with original-audio clocks and no edits.

Use two CTC contexts to detect occurrence drift, then measure periodicity at
the final word. Neither CTC nor periodicity certifies the supplied lyric or
the identity of a sustained singer. All output remains review-only.
"""
import math

from reviewer_phrase_alignment import align_phrase, compare_occurrence_anchors
from reviewer_endpoint_interval import measure
from reviewer_shadow import tokens


def _valid_alignment(result, text, window):
    if result.get("status") != "aligned_hypothesis" or result.get("clock") != "original_mix_decoded":
        return False
    words = result.get("words", [])
    if tokens(" ".join(w.get("word", "") for w in words)) != tokens(text):
        return False
    previous = window["start"]
    for word in words:
        start, end = word.get("global_start"), word.get("global_end")
        if not all(type(v) in (int, float) and math.isfinite(v) for v in (start, end)):
            return False
        if not previous <= start < end <= window["end"]:
            return False
        previous = end
    return bool(words)


def investigate(audio, segments, index, duration, *, text_decision=None,
                conditional=False, aligner=align_phrase, endpoint_measure=measure):
    """Investigate one phrase; `conditional` allows an explicit local CTC probe.

    Conditional runs never claim to have independently recognized the text.
    Snapshot identity/validity must be checked by the caller before execution.
    """
    if type(index) is not int or not 0 <= index < len(segments):
        raise ValueError("invalid_line_index")
    line = segments[index]
    result = {"line_index": index, "before": dict(line), "status": "NEEDS_REVIEW",
              "automatic_apply_allowed": False, "timing_verified": False,
              "perceptual_accuracy_measured": False, "provider_calls": 0,
              "clock": "original_mix_decoded", "investigations": [], "hypotheses": []}
    if line.get("locked") or line.get("operator_locked"):
        return {**result, "reason": "protected_line"}
    decision = text_decision or {}
    supported = decision.get("status") in {"TEXT_SUPPORTED", "TEXT_PROPOSAL"}
    if not supported and not conditional:
        return {**result, "reason": "resolve_text_before_forced_alignment",
                "next_action": "independent_audio_listening"}
    text = decision.get("proposed_text", line["text"]) if supported else line["text"]
    result.update(conditioning_text=text, text_independently_supported=supported,
                  text_correctness_certified=False,
                  text_change_requires_realignment=decision.get("status") == "TEXT_PROPOSAL")
    # Neighbor clocks are search constraints, not verified vocal boundaries.
    floor = max([s["end"] for i, s in enumerate(segments) if i != index and s["start"] < line["start"]] or [0.])
    ceiling = min([s["start"] for i, s in enumerate(segments) if i != index and s["start"] >= line["start"]] or [duration])
    if floor > line["start"] or ceiling < line["end"]:
        return {**result, "reason": "overlapping_occurrences_require_review"}
    narrow = {"start": max(floor, line["start"] - 1.), "end": min(ceiling, line["end"] + 1.)}
    wide = {"start": max(floor, line["start"] - 3.), "end": min(ceiling, line["end"] + 3.)}
    if narrow == wide:
        return {**result, "reason": "no_independent_context_within_neighbors"}
    if any(not 1 <= w["end"] - w["start"] <= 24 for w in (narrow, wide)):
        return {**result, "reason": "phrase_exceeds_acoustic_window_budget"}
    result["search_bounds"] = {"floor": floor, "ceiling": ceiling, "source": "unverified_neighbor_timeline"}
    alignments = []
    for window in (narrow, wide):
        try:
            alignment = aligner(audio, text, window)
        except Exception as exc:
            return {**result, "reason": "alignment_tool_failed", "error_type": type(exc).__name__}
        result["investigations"].append({"action": "align_phrase", "result": alignment})
        if not _valid_alignment(alignment, text, window):
            return {**result, "reason": "alignment_failed_or_invalid_clock"}
        alignments.append(alignment)
    anchors = compare_occurrence_anchors(*alignments)
    result["occurrence_check"] = anchors
    if not anchors["same_occurrence_supported"]:
        return {**result, "reason": "occurrence_not_stable"}
    if any(a.get("boundary_near_clip_edge") for a in alignments):
        return {**result, "reason": "word_truncated_at_context_edge"}
    first, second = (a["words"] for a in alignments)
    start_shift = abs(first[0]["global_start"] - second[0]["global_start"])
    end_shift = abs(first[-1]["global_end"] - second[-1]["global_end"])
    result["boundary_stability"] = {"start_shift_seconds": start_shift, "end_shift_seconds": end_shift,
                                   "tolerance_seconds": .15, "confidence_probability": None}
    if max(start_shift, end_shift) > .15:
        return {**result, "reason": "boundary_changes_with_context"}
    result["hypotheses"] = [{"kind": "aligned_word_span", "start": second[0]["global_start"],
                             "end": second[-1]["global_end"], "visual_endpoint_certified": False}]
    try:
        endpoint = endpoint_measure(audio, alignments[-1], next_occurrence_start=ceiling)
    except Exception as exc:
        endpoint = {"status": "tool_error", "error_type": type(exc).__name__}
    result["investigations"].append({"action": "measure_final_word_periodicity", "result": endpoint})
    interval = endpoint.get("interval")
    if (endpoint.get("status") == "periodicity_boundary_observed" and isinstance(interval, (list, tuple))
            and len(interval) == 2 and all(type(v) in (int, float) and math.isfinite(v) for v in interval)
            and second[-1]["global_start"] <= interval[0] <= interval[1] <= wide["end"]):
        if interval[1] < second[-1]["global_end"] - .15:
            # A final consonant may outlast periodicity; the earlier vowel
            # boundary is not permission to hide a still-sung word.
            return {**result, "reason": "periodicity_ends_before_aligned_word",
                    "next_action": "inspect_final_consonant_or_voice_overlap",
                    "boundary_difference_seconds": second[-1]["global_end"] - interval[1]}
        result["hypotheses"].append({"kind": "periodicity_end_interval", "interval": list(interval),
                                    "target_voice_certified": False, "visual_endpoint_certified": False})
    result.update(status="ACOUSTIC_HYPOTHESES" if supported else "CONDITIONAL_ACOUSTIC_HYPOTHESES",
                  reason="review_word_span_and_sustain_separately",
                  next_action="compare_with_original_audio_and_render")
    return result
