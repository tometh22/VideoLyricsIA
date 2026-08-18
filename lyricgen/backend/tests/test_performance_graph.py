import math
import itertools

import pytest

from performance_graph import (
    BoundaryState,
    PhraseComposition,
    VocalTaxonomy,
    build_performance_graph,
    to_legacy_acoustic_structure,
)


def _boundary(continue_score=-4.0, subevent_score=-4.0, phrase_score=-4.0):
    return {"raw_scores": {
        "CONTINUE": continue_score,
        "SUBEVENT": subevent_score,
        "PHRASE": phrase_score,
    }}


def test_builds_phrase_and_subevent_hierarchy_without_text_cardinality():
    primitives = [
        {"start": 0.00, "end": .45, "features": {
            "lexical_probability": .95, "sung_lead_probability": .9,
        }},
        {"start": .48, "end": .82, "features": {
            "lexical_probability": .90, "sung_lead_probability": .9,
        }},
        {"start": .88, "end": 1.60, "features": {
            "nonlexical_probability": .95, "sung_lead_probability": .8,
        }},
        {"start": 2.00, "end": 2.60, "features": {
            "lexical_probability": .9, "sung_lead_probability": .9,
        }},
    ]
    graph = build_performance_graph(primitives, [
        _boundary(continue_score=5),
        _boundary(subevent_score=5),
        _boundary(phrase_score=5),
    ])

    assert [boundary.state for boundary in graph.boundaries] == [
        BoundaryState.CONTINUE,
        BoundaryState.SUBEVENT,
        BoundaryState.PHRASE,
    ]
    assert len(graph.subevents) == 3
    assert len(graph.phrases) == 2
    first = graph.phrases[0]
    assert first.composition is PhraseComposition.LEXICAL_PLUS_VOCALIZATION
    assert len(first.subevent_ids) == 2
    assert first.acoustic_end == 1.6
    assert first.display_end == 2.0
    assert graph.phrases[1].acoustic_start == 2.0


def test_exact_cardinality_posterior_is_independent_of_n_best():
    primitives = [
        {"start": index * 1.2, "end": index * 1.2 + .8,
         "features": {"voicing": .8, "harmonicity": .7}}
        for index in range(5)
    ]
    boundaries = [
        _boundary(1.1, .2, -.1),
        _boundary(-.2, .8, .3),
        _boundary(.1, -.3, 1.0),
        _boundary(.4, .5, .2),
    ]
    one = build_performance_graph(primitives, boundaries, n_best=1)
    many = build_performance_graph(primitives, boundaries, n_best=32)

    assert one.cardinality_posterior == many.cardinality_posterior
    assert one.raw_score == many.raw_score
    assert [boundary.state for boundary in one.boundaries] == [
        boundary.state for boundary in many.boundaries
    ]
    assert math.isclose(sum(one.cardinality_posterior.values()), 1.0, abs_tol=1e-12)
    assert set(one.cardinality_posterior) == {1, 2, 3, 4, 5}
    for boundary in one.boundaries:
        assert math.isclose(sum(boundary.state_posterior.values()), 1.0, abs_tol=1e-12)


def test_forward_backward_matches_exhaustive_boundary_enumeration():
    primitives = [
        {"start": 0.0, "end": .7, "features": {"phrase_cohesion": .2}},
        {"start": 1.0, "end": 1.8, "features": {"phrase_cohesion": .8}},
        {"start": 2.1, "end": 3.0, "features": {"phrase_cohesion": .4}},
        {"start": 3.5, "end": 4.2, "features": {"phrase_cohesion": .6}},
    ]
    boundary_rows = [
        _boundary(.4, -.2, .1),
        _boundary(-.3, .7, .2),
        _boundary(.1, .0, .8),
    ]
    graph = build_performance_graph(primitives, boundary_rows, n_best=1)

    raw_rows = [row["raw_scores"] for row in boundary_rows]
    mass_by_count = {}
    total_mass = 0.0
    boundary_mass = [dict.fromkeys(BoundaryState, 0.0) for _ in raw_rows]
    for states in itertools.product(BoundaryState, repeat=len(raw_rows)):
        spans = []
        left = 0
        for index, state in enumerate(states):
            if state is BoundaryState.PHRASE:
                spans.append((left, index))
                left = index + 1
        spans.append((left, len(primitives) - 1))
        score = sum(raw_rows[index][state.value] for index, state in enumerate(states))
        # Mirrors the documented transparent phrase-span potential.
        for start, end in spans:
            duration = primitives[end]["end"] - primitives[start]["start"]
            internal_cohesion = sum(
                min(
                    primitives[index]["features"]["phrase_cohesion"],
                    primitives[index + 1]["features"]["phrase_cohesion"],
                )
                for index in range(start, end)
            )
            score += (
                -.28 - 2.40 * max(0.0, .18 - duration)
                - min(.18, .18 * max(0.0, duration - 10.0) ** 1.35)
                + .18 * internal_cohesion
            )
        mass = math.exp(score)
        total_mass += mass
        mass_by_count[len(spans)] = mass_by_count.get(len(spans), 0.0) + mass
        for index, state in enumerate(states):
            boundary_mass[index][state] += mass

    expected_count = {
        count: mass / total_mass for count, mass in mass_by_count.items()
    }
    assert graph.cardinality_posterior == pytest.approx(expected_count, abs=1e-10)
    for index, boundary in enumerate(graph.boundaries):
        expected_boundary = {
            state.value: boundary_mass[index][state] / total_mass
            for state in BoundaryState
        }
        assert boundary.state_posterior == pytest.approx(expected_boundary, abs=1e-10)


def test_all_v6_taxonomy_classes_are_representable():
    classes = list(VocalTaxonomy)
    primitives = []
    for index, taxonomy in enumerate(classes):
        primitives.append({
            "start": index * 2.0,
            "end": index * 2.0 + .8,
            "features": {"taxonomy_scores": {
                item.value: 8.0 if item is taxonomy else -8.0
                for item in classes
            }},
        })
    graph = build_performance_graph(
        primitives,
        [_boundary(phrase_score=8.0) for _ in range(len(primitives) - 1)],
    )
    assert [phrase.taxonomy for phrase in graph.phrases] == classes
    assert [subevent.taxonomy for subevent in graph.subevents] == classes


def test_sustained_and_vocalization_compositions_are_distinct():
    graph = build_performance_graph([
        {"start": 0, "end": 1, "features": {
            "nonlexical_probability": .9, "sustained_probability": .05,
        }},
        {"start": 2, "end": 5, "features": {
            "nonlexical_probability": .7, "sustained_probability": .98,
        }},
    ], [_boundary(phrase_score=8)])
    assert [phrase.composition for phrase in graph.phrases] == [
        PhraseComposition.VOCALIZATION,
        PhraseComposition.SUSTAINED,
    ]


def test_ids_are_deterministic_and_not_affected_by_diagnostic_n_best():
    primitives = [
        {"start": 10.1234564, "end": 10.8, "features": {"voicing": .8}},
        {"start": 11.0, "end": 11.9, "features": {"voicing": .7}},
        {"start": 12.1, "end": 13.0, "features": {"voicing": .9}},
    ]
    boundaries = [_boundary(subevent_score=5), _boundary(phrase_score=5)]
    first = build_performance_graph(primitives, boundaries, n_best=1)
    second = build_performance_graph(primitives, boundaries, n_best=10)

    assert first.id == second.id
    assert [item.id for item in first.primitives] == [item.id for item in second.primitives]
    assert [item.id for item in first.subevents] == [item.id for item in second.subevents]
    assert [item.id for item in first.phrases] == [item.id for item in second.phrases]
    assert [item.id for item in first.boundaries] == [item.id for item in second.boundaries]

    differently_segmented = build_performance_graph(
        primitives, [_boundary(phrase_score=5), _boundary(phrase_score=5)],
    )
    assert [item.id for item in first.primitives] == [
        item.id for item in differently_segmented.primitives
    ]
    assert first.id != differently_segmented.id


def test_raw_scores_never_masquerade_as_calibrated_confidence():
    graph = build_performance_graph([
        {"start": 0, "end": 1, "features": {
            "taxonomy_scores": {"SUNG_LEAD": 100.0},
            "lexical_probability": 1.0,
        }},
        {"start": 1.2, "end": 2, "features": {
            "taxonomy_scores": {"NONLEXICAL": 100.0},
            "nonlexical_probability": 1.0,
        }},
    ], [_boundary(subevent_score=100.0)])

    assert graph.raw_score > 0
    assert graph.calibrated_confidence is None
    assert graph.calibration_status == "uncalibrated"
    assert all(item.calibrated_confidence is None for item in graph.boundaries)
    assert all(item.calibrated_confidence is None for item in graph.subevents)
    assert all(item.calibrated_confidence is None for item in graph.phrases)


def test_legacy_adapter_preserves_hierarchy_and_confidence_separation():
    graph = build_performance_graph([
        {"start": 60.85, "end": 61.6, "features": {
            "lexical_probability": .95, "sung_lead_probability": .9,
        }},
        {"start": 61.7, "end": 63.5, "features": {
            "nonlexical_probability": .95, "sung_lead_probability": .8,
        }},
        {"start": 63.77, "end": 67.04, "features": {
            "lexical_probability": .9, "sung_lead_probability": .9,
        }},
    ], [_boundary(subevent_score=6), _boundary(phrase_score=6)], n_best=5)
    legacy = to_legacy_acoustic_structure(graph)

    assert legacy["accepted"] is True
    assert legacy["diagnostics"]["cardinality_inference"] == "exact_forward_backward"
    assert legacy["diagnostics"]["n_best_affects_inference"] is False
    assert legacy["automatic_apply_allowed"] is False
    assert legacy["best_partition"]["event_count"] == 2
    first = legacy["best_partition"]["events"][0]
    assert first["composition"] == "lexical_plus_vocalization"
    assert len(first["acoustic_event_ids"]) == 2
    assert first["acoustic_end"] == 63.5
    assert first["display_end"] == 63.77
    assert first["confidence"] is None
    assert first["confidence_kind"] == "uncalibrated"
    assert math.isclose(
        sum(legacy["cardinality_posterior"].values()), 1.0, abs_tol=1e-9,
    )


def test_neutral_boundaries_do_not_collect_singleton_fragmentation_rewards():
    primitives = [
        {"start": index * 1.05, "end": index * 1.05 + .9, "features": {
            "phrase_cohesion": .95, "motif_recurrence": .9,
            "lexical_probability": .8,
        }}
        for index in range(4)
    ]
    neutral = [_boundary(0.0, 0.0, 0.0) for _ in range(3)]
    graph = build_performance_graph(primitives, neutral, n_best=16)

    assert len(graph.phrases) == 1
    assert graph.alternatives[0].phrase_count == 1

    strong_phrase = build_performance_graph(
        primitives,
        [_boundary(0.0, 0.0, 8.0), *neutral[1:]],
        n_best=16,
    )
    assert len(strong_phrase.phrases) == 2


def test_long_neutral_phrase_is_not_split_by_duration_prior():
    primitives = [
        {
            "start": index * 1.05,
            "end": index * 1.05 + .9,
            "features": {
                "phrase_cohesion": .5,
                "motif_recurrence": 0.0,
                "lexical_probability": .8,
            },
        }
        for index in range(16)
    ]
    graph = build_performance_graph(
        primitives,
        [_boundary(0.0, 0.0, 0.0) for _ in range(15)],
        n_best=32,
    )

    assert len(graph.phrases) == 1
    assert graph.alternatives[0].phrase_count == 1
    assert graph.calibrated_confidence is None


def test_every_n_best_event_preserves_taxonomy_composition_and_type_evidence():
    graph = build_performance_graph([
        {"start": 0, "end": .8, "features": {
            "lexical_probability": .9, "sung_lead_probability": .95,
        }},
        {"start": 1, "end": 2.2, "features": {
            "nonlexical_probability": .9, "sustained_probability": .8,
        }},
        {"start": 2.4, "end": 3.1, "features": {
            "sung_crowd_probability": .85, "nonlexical_probability": .7,
        }},
    ], [_boundary(0, 0, 0), _boundary(0, 0, 0)], n_best=8)
    legacy = to_legacy_acoustic_structure(graph)

    assert len(legacy["n_best"]) > 1
    for partition in legacy["n_best"]:
        for event in partition["events"]:
            assert event["taxonomy"] in {item.value for item in VocalTaxonomy}
            assert event["composition"] in {item.value for item in PhraseComposition}
            assert event["type_posterior"]
            assert event["raw_type_scores"]
            assert event["raw_composition_scores"]


def test_empty_graph_and_invalid_regions_fail_deterministically():
    empty = build_performance_graph([])
    assert empty.cardinality_posterior == {0: 1.0}
    assert empty.phrases == ()
    assert to_legacy_acoustic_structure(empty)["reason"] == "no_vocal_events"

    with pytest.raises(ValueError, match="invalid acoustic range"):
        build_performance_graph([{"start": 1.0, "end": 1.0}])
    with pytest.raises(ValueError, match="must not overlap"):
        build_performance_graph([
            {"start": 0.0, "end": 1.0},
            {"start": .9, "end": 1.5},
        ])


def test_pericos_outro_fixture_groups_primitives_into_six_editable_phrases():
    lexical = {"lexical_probability": .95, "sung_lead_probability": .9}
    vocal = {"nonlexical_probability": .95, "sung_lead_probability": .9}
    mixed = {
        "lexical_probability": .9, "nonlexical_probability": .85,
        "sung_lead_probability": .9,
    }
    primitives = [
        {"start": 60.85, "end": 61.55, "features": lexical},
        {"start": 61.62, "end": 63.77, "features": vocal},
        {"start": 63.77, "end": 67.04, "features": mixed},
        {"start": 67.05, "end": 73.17, "features": mixed},
        {"start": 73.18, "end": 73.85, "features": lexical},
        {"start": 73.90, "end": 75.65, "features": vocal},
        {"start": 75.65, "end": 75.75, "features": vocal},
        {"start": 79.31, "end": 83.27, "features": {
            **vocal, "sustained_probability": .99,
        }},
    ]
    boundaries = [
        _boundary(subevent_score=8), _boundary(phrase_score=8),
        _boundary(phrase_score=8), _boundary(phrase_score=8),
        _boundary(subevent_score=8), _boundary(phrase_score=8),
        _boundary(phrase_score=8),
    ]
    graph = build_performance_graph(primitives, boundaries)

    assert len(graph.phrases) == 6
    assert [round(item.acoustic_start, 2) for item in graph.phrases] == [
        60.85, 63.77, 67.05, 73.18, 75.65, 79.31,
    ]
    assert graph.phrases[0].composition is PhraseComposition.LEXICAL_PLUS_VOCALIZATION
    assert graph.phrases[3].composition is PhraseComposition.LEXICAL_PLUS_VOCALIZATION
    assert graph.phrases[-1].composition is PhraseComposition.SUSTAINED
    assert graph.phrases[-1].acoustic_end == 83.27
    assert all(item.acoustic_start < 83.27 for item in graph.phrases)
