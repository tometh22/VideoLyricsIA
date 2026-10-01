from copy import deepcopy

from structural_t4_shadow import build_structural_t4_shadow


def _segment(start, end, word_end, **extra):
    return {
        "start": start,
        "end": end,
        "text": "line",
        "words": [{"word": "line", "start": start, "end": word_end, "score": .9}],
        **extra,
    }


def _endpoint(index, display_end, word_end, candidate_end):
    return {
        "segment_index": index,
        "source_display_end": display_end,
        "source_phonetic_end": word_end,
        "candidate_end": candidate_end,
        "families": ["provider_words", "stable_pitch"],
        "proof_sha256": "b" * 64,
    }


def test_proposes_independently_attested_end_without_mutating_source():
    segments = [
        _segment(
            1.0, 2.0, 2.6,
            timing_endpoint_attestation=_endpoint(0, 2.0, 2.6, 2.7),
        ),
        _segment(4.0, 5.0, 4.8),
    ]
    frozen = deepcopy(segments)

    report = build_structural_t4_shadow(segments)

    assert segments == frozen
    assert report["proposal_count"] == 1
    assert report["proposals"][0]["action"] == "extend_display_to_independent_endpoint"
    assert report["proposals"][0]["candidate_display_end"] == 2.7
    assert report["proposals"][0]["independent_endpoint_attested"] is True
    assert report["automatic_timing_change_allowed"] is False


def test_crossing_next_line_abstains_without_occurrence_attestation():
    report = build_structural_t4_shadow([
        _segment(
            1.0, 2.0, 3.3,
            timing_endpoint_attestation=_endpoint(0, 2.0, 3.3, 3.4),
        ),
        _segment(3.0, 4.0, 3.8),
    ])

    assert report["proposal_count"] == 0
    assert report["rows"][0]["reason"] == "occurrence_identity_required"


def test_crossing_next_line_is_review_only_with_hash_bound_attestation():
    report = build_structural_t4_shadow([
        _segment(
            1.0, 2.0, 3.3,
            timing_endpoint_attestation=_endpoint(0, 2.0, 3.3, 3.4),
            occurrence_identity={
                "same_occurrence": True,
                "from_index": 0,
                "to_index": 1,
                "proof_sha256": "a" * 64,
            },
        ),
        _segment(3.0, 4.0, 3.8),
    ])

    assert report["proposal_count"] == 1
    assert report["proposals"][0]["candidate_display_end"] == 3.4
    assert report["proposals"][0]["occurrence_identity_attested"] is True
    assert report["proposals"][0]["automatic_timing_change_allowed"] is False


def test_shared_word_line_boundary_is_not_self_certified():
    report = build_structural_t4_shadow([
        _segment(1.0, 3.0, 3.0),
        _segment(3.0, 4.0, 3.8),
    ])

    assert report["proposal_count"] == 0
    assert report["rows"][0]["diagnosis"] == "upstream_shared_word_line_boundary"
    assert report["rows"][0]["reason"] == "display_boundary_inherited_from_next_line"


def test_single_word_clock_is_diagnostic_but_cannot_propose_visible_change():
    report = build_structural_t4_shadow([
        _segment(1.0, 2.0, 2.7),
    ])

    assert report["proposal_count"] == 0
    assert report["rows"][0]["diagnosis"] == "card_ends_before_trusted_last_word"
    assert report["rows"][0]["reason"] == "independent_endpoint_attestation_required"


def test_locked_segment_never_receives_a_proposal():
    report = build_structural_t4_shadow([
        _segment(1.0, 2.0, 2.7, locked=True),
    ])

    assert report["proposal_count"] == 0
    assert report["rows"][0]["reason"] == "operator_locked"
