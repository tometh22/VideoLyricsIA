from lyric_content_policy import (
    classify_acoustic_window,
    editorial_display_decision,
    route_omission_window,
)


def test_long_vocalization_is_parenthesized_but_never_auto_inserted():
    decision = editorial_display_decision(
        "oh oh", provider_kind="vocalization", duration_s=1.2,
    )

    assert decision["display"] == "parenthesize"
    assert decision["safe_for_auto_insert"] is False


def test_ambiguous_speech_requires_review():
    decision = editorial_display_decision(
        "quiero decirles algo", provider_kind="speech",
    )

    assert decision["display"] == "review"
    assert decision["review_required"] is True


def test_compositional_speech_is_displayed_when_attested():
    decision = editorial_display_decision(
        "quiero decirles algo",
        provider_kind="speech",
        compositional_speech=True,
    )

    assert decision["display"] == "normal"


def test_acoustic_lexical_window_requires_independent_text_consensus():
    route = classify_acoustic_window({
        "best_partition": {
            "events": [{"start": 1.0, "end": 2.0, "taxonomy": "SUNG_LEAD"}],
        },
    })

    assert route["content_type"] == "lexical_candidate"
    assert route["display"] == "review"
    assert route["allow_lexical_ranking"] is True
    assert route["safe_for_auto_insert"] is False


def test_nonlexical_omission_window_abstains_before_lexical_ranking():
    route = classify_acoustic_window({
        "best_partition": {
            "events": [{"start": 1.0, "end": 2.2, "taxonomy": "NONLEXICAL"}],
        },
    })

    decision = route_omission_window(
        {"reasons": ["voiced_gap"]}, route,
    )

    assert decision["omission_only"] is True
    assert decision["content_type"] == "melodic_vocalization"
    assert decision["allow_lexical_ranking"] is False
    assert decision["safe_for_auto_insert"] is False


def test_mixed_risk_window_is_not_removed_by_omission_gate():
    route = classify_acoustic_window({"best_partition": {"events": []}})

    decision = route_omission_window(
        {"reasons": ["voiced_gap", "text_mismatch"]}, route,
    )

    assert decision["omission_only"] is False
    assert decision["allow_lexical_ranking"] is True
