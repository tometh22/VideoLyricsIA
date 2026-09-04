from copy import deepcopy

from operator_review_proposals import build_operator_review_proposal
from spanish_orthography import analyze_spanish_orthography


def test_detects_umg_diacritics_typo_and_preserves_uppercase_without_reference():
    segments = [
        {"start": 74.367, "end": 75.2, "text": "TENDRAS"},
        {"start": 75.8, "end": 76.6, "text": "JAMAS"},
        {"start": 186.7, "end": 188.0, "text": "AVENTRUA"},
    ]
    original = deepcopy(segments)

    report = analyze_spanish_orthography(segments)

    assert segments == original
    assert report["reference_required"] is False
    assert report["automatic_apply_allowed"] is False
    assert [(row["actual"], row["expected"]) for row in report["findings"]] == [
        ("TENDRAS", "TENDRÁS"),
        ("JAMAS", "JAMÁS"),
        ("AVENTRUA", "AVENTURA"),
    ]
    assert [row["proposed_segments"][0]["text"] for row in report["candidates"]] == [
        "TENDRÁS", "JAMÁS", "AVENTURA",
    ]
    assert all(row["automatic_apply_allowed"] is False for row in report["candidates"])


def test_regular_future_is_review_only_and_ambiguous_short_homographs_abstain():
    report = analyze_spanish_orthography([
        {"start": 1, "end": 2, "text": "CANTARAS SI TU CAMINO"},
    ])

    assert [(row["actual"], row["expected"], row["confidence"])
            for row in report["findings"]] == [
        ("CANTARAS", "CANTARÁS", "medium"),
    ]
    assert report["candidates"][0]["proposed_segments"][0]["text"] == (
        "CANTARÁS SI TU CAMINO"
    )


def test_groups_multiple_safe_changes_in_one_line_into_one_editor_action():
    report = analyze_spanish_orthography([
        {"start": 2, "end": 4, "text": "Jamas tendras aventura"},
    ])

    assert report["finding_count"] == 2
    assert report["candidate_count"] == 1
    assert report["candidates"][0]["proposed_segments"][0]["text"] == (
        "Jamás tendrás aventura"
    )


def test_deterministic_candidate_is_a_one_click_operator_proposal():
    segments = [{"start": 2, "end": 4, "text": "JAMAS"}]
    report = analyze_spanish_orthography(segments)

    proposal, telemetry = build_operator_review_proposal(
        segments, text_candidates=report["candidates"],
    )

    assert telemetry["by_type"]["text"] == 1
    assert proposal["review_only"] is True
    assert proposal["operator_suggestion_only"] is True
    assert proposal["automatic_apply_allowed"] is False
    assert proposal["windows"][0]["proposed_segments"][0]["text"] == "JAMÁS"
