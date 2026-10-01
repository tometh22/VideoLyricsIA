from operator_review_proposals import build_operator_review_proposal


def _timing(identifier="t1", start=1.0, end=2.5, impact=500):
    current = {"_id": 1, "start": start, "end": 2.0, "text": "line"}
    return {
        "kind": "operator_review_candidate", "id": identifier,
        "start": start, "end": end, "reasons": ["timing"],
        "current_segments": [current],
        "proposed_segments": [{**current, "end": end}],
        "suggestion_type": "timing", "confidence": "high",
        "impact_ms": impact, "automatic_apply_allowed": False,
    }


def test_builds_human_only_batch_and_orders_without_auto_apply():
    proposal, telemetry = build_operator_review_proposal(
        [{"_id": 1, "start": 1, "end": 2, "text": "line"}],
        timing_candidates=[_timing()],
    )

    assert proposal["operator_suggestion_only"] is True
    assert proposal["automatic_apply_allowed"] is False
    assert proposal["windows"][0]["suggestion_type"] == "timing"
    assert telemetry["by_type"]["timing"] == 1


def test_text_needs_two_families_and_complete_parent():
    raw = {
        "kind": "review_proposal_candidate", "id": "text-1",
        "parent_window_id": "gap-1", "start": 4, "end": 6,
        "reasons": ["voiced_gap"],
        "current_segments": [{"start": 4, "end": 5, "text": "old"}],
        "proposed_segments": [{"start": 4, "end": 5, "text": "new"}],
        "source_families": ["whisper", "gemini_audio"],
    }
    proposal, _ = build_operator_review_proposal(
        [], text_candidates=[raw], complete_parent_ids={"gap-1"},
    )
    assert proposal["windows"][0]["suggestion_type"] == "text"

    blocked, telemetry = build_operator_review_proposal(
        [], text_candidates=[{**raw, "source_families": ["whisper"]}],
        complete_parent_ids={"gap-1"},
    )
    assert blocked is None
    assert telemetry["proposal_count"] == 0


def test_overlapping_suggestions_keep_highest_impact():
    proposal, telemetry = build_operator_review_proposal(
        [], timing_candidates=[
            _timing("small", impact=200), _timing("large", impact=900),
        ],
    )
    assert [item["id"] for item in proposal["windows"]] == ["large"]
    assert telemetry["declined_overlap_count"] == 1
