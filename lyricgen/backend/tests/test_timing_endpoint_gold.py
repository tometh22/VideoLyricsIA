import pytest

from timing_endpoint_gold import (
    GoldValidationError,
    clopper_pearson_lower,
    evaluate_experiment,
    validate_gold,
    zero_error_sample_size,
)


def _gold(index, *, label_class="short", split="test", mode="blind", artist=None):
    current = 1.0 if label_class == "short" else 1.6
    return {
        "schema": "timing-endpoint-gold-v1",
        "label_id": f"label-{index}",
        "job_id": f"job-{index}",
        "line_id": f"line-{index}",
        "audio_sha256": f"sha-{index}",
        "song_group_id": f"song-{index}",
        "artist_group_id": artist or f"artist-{index}",
        "recording_group_id": f"recording-{index}",
        "evaluation_unit_id": f"unit-{index}",
        "reviewer_id": "reviewer-pseudonym",
        "annotation_version": "v1",
        "label_class": label_class,
        "sample_role": "control" if label_class == "correct" else "random",
        "annotation_mode": mode,
        "engine_end_visible": mode != "blind",
        "vocal_attribution": "target_singer",
        "measurement_source": "rendered_video",
        "split": split,
        "current_end_s": current,
        "sung_end_s": 1.7,
        "acceptable_end_min_s": 1.5,
        "acceptable_end_max_s": 1.9,
    }


def _prediction(index, *, decision="apply", end=1.7):
    row = {
        "schema": "timing-endpoint-prediction-v1",
        "label_id": f"label-{index}",
        "decision": decision,
        "proposer_family": "frozen-audio-encoder-v1",
        "selector_family": "regularized-selector-v1",
    }
    if decision == "apply":
        row["proposed_end_s"] = end
    else:
        row["abstention_reason"] = "insufficient_signal"
    return row


def test_99_percent_zero_error_gate_needs_299_independent_units():
    assert zero_error_sample_size(0.99) == 299
    assert clopper_pearson_lower(50, 50) == pytest.approx(0.941845, abs=1e-6)
    assert clopper_pearson_lower(299, 299) >= 0.99


def test_gold_rejects_text_and_group_leakage():
    with pytest.raises(GoldValidationError, match="lyric text"):
        validate_gold([{**_gold(1), "text": "must not be copied into metrics"}])
    rows = [_gold(1, split="train", artist="same"), _gold(2, split="test", artist="same")]
    with pytest.raises(GoldValidationError, match="crosses splits"):
        validate_gold(rows)


def test_blind_label_requires_engine_timing_hidden():
    with pytest.raises(GoldValidationError, match="engine_end_visible=false"):
        validate_gold([{**_gold(1), "engine_end_visible": True}])


def test_report_measures_applied_precision_recall_harm_and_abstention():
    gold = [
        _gold(1, label_class="short"),
        _gold(2, label_class="short"),
        _gold(3, label_class="correct"),
        _gold(4, label_class="ambiguous"),
    ]
    predictions = [
        _prediction(1, end=1.7),
        _prediction(2, decision="abstain"),
        _prediction(3, end=2.4),
        _prediction(4, decision="abstain"),
    ]
    report = evaluate_experiment(
        gold, predictions, threshold_frozen_before_test=True,
        review_rows=[
            {"job_id": "job-1", "task_type": "timing", "active_minutes": 2.5},
            {"job_id": "job-1", "task_type": "text", "active_minutes": 1.0},
        ],
    )
    test = report["by_split"]["test"]
    assert test["applied_changes"] == 2
    assert test["successful_changes"] == 1
    assert test["applied_change_precision"] == 0.5
    assert test["short_recall"] == 0.5
    assert test["harmed_controls"] == 1
    assert test["abstentions"] == 2  # Includes explicit abstention on ambiguity.
    assert report["human_review"]["minutes_per_song"] == {"job-1": 3.5}
    assert report["automatic_apply_allowed"] is False


def test_same_model_family_is_reported_not_misrepresented_as_independent():
    gold = [_gold(1)]
    prediction = _prediction(1)
    prediction["selector_family"] = prediction["proposer_family"]
    report = evaluate_experiment(gold, [prediction], threshold_frozen_before_test=True)
    assert report["model_components"][0]["independent_families"] is False


def test_per_line_unit_ids_cannot_certify_one_song_or_artist():
    gold = [_gold(i, artist="one-artist") for i in range(300)]
    report = evaluate_experiment(gold, [_prediction(i) for i in range(300)],
                                 threshold_frozen_before_test=True)
    test = report["by_split"]["test"]
    assert test["applied_change_precision"] == 1
    assert test["applied_change_precision_lower_95"] is None
    assert test["evaluation_units_with_changes"] == 1
    assert test["unit_precision_lower_95"] == pytest.approx(0.05)
    assert report["exploratory_gate"]["passed"] is False
    assert report["production_99_gate"]["passed"] is False


@pytest.mark.parametrize("field", ["song_group_id", "recording_group_id", "job_id", "audio_sha256", "evaluation_unit_id"])
def test_dependency_alias_cannot_cross_splits(field):
    rows = [_gold(1, split="train"), _gold(2, split="test")]
    rows[1][field] = rows[0][field]
    with pytest.raises(GoldValidationError, match="crosses splits"):
        validate_gold(rows)


def test_abstained_bridge_still_connects_dependency_groups():
    gold = [_gold(i) for i in range(3)]
    gold[1]["song_group_id"] = gold[0]["song_group_id"]
    gold[2]["recording_group_id"] = gold[1]["recording_group_id"]
    report = evaluate_experiment(gold, [_prediction(0), _prediction(1, decision="abstain"), _prediction(2)],
                                 threshold_frozen_before_test=True)
    assert report["by_split"]["test"]["evaluation_units_with_changes"] == 1


def test_untouched_controls_do_not_dilute_conditional_harm():
    gold = [_gold(i, label_class="correct") for i in range(100)]
    predictions = [_prediction(0, end=2.4)] + [_prediction(i, decision="abstain") for i in range(1, 100)]
    report = evaluate_experiment(gold, predictions, threshold_frozen_before_test=True)
    test = report["by_split"]["test"]
    assert test["correct_controls"] == 100
    assert test["applied_controls"] == 1
    assert test["control_harm_rate"] == 1
    assert test["control_harm_upper_95"] == 1


def test_zero_harm_repeated_lines_are_not_zero_risk():
    gold = [_gold(i, label_class="correct", artist="same") for i in range(100)]
    report = evaluate_experiment(gold, [_prediction(i) for i in range(100)],
                                 threshold_frozen_before_test=True)
    test = report["by_split"]["test"]
    assert test["harmed_controls"] == 0
    assert test["control_units_with_changes"] == 1
    assert test["control_harm_upper_95"] == pytest.approx(0.95)


def test_no_op_does_not_count_as_a_safe_change():
    report = evaluate_experiment([_gold(1, label_class="correct")], [_prediction(1, end=1.6)],
                                 threshold_frozen_before_test=True)
    test = report["by_split"]["test"]
    assert test["requested_applications"] == 1
    assert test["no_op_applications"] == 1
    assert test["applied_changes"] == 0
    assert test["control_harm_upper_95"] is None


@pytest.mark.parametrize("ambiguous_field", ["label_class", "vocal_attribution"])
def test_ambiguity_cannot_be_silently_excluded_when_applied(ambiguous_field):
    gold = {**_gold(1), ambiguous_field: "ambiguous"}
    report = evaluate_experiment([gold], [_prediction(1)], threshold_frozen_before_test=True)
    assert report["by_split"]["test"]["unjudgable_applications"] == 1
    assert "changes_applied_to_ambiguous_labels" in report["exploratory_gate"]["blockers"]


def test_sufficient_independent_safe_changes_can_pass_exploration_not_certification():
    gold = [_gold(i, label_class="short" if i < 60 else "correct") for i in range(120)]
    gold.append(_gold(120, label_class="ambiguous"))
    predictions = [_prediction(i) for i in range(120)] + [_prediction(120, decision="abstain")]
    report = evaluate_experiment(gold, predictions, threshold_frozen_before_test=True)
    assert report["exploratory_gate"]["passed"] is True
    assert report["production_99_gate"]["passed"] is False
    assert report["automatic_apply_allowed"] is False


def test_training_controls_cannot_satisfy_test_design():
    gold = [_gold(1), _gold(2, label_class="correct", split="train")]
    report = evaluate_experiment(gold, [_prediction(1), _prediction(2)], threshold_frozen_before_test=True)
    assert "missing_test_control_sample" in report["exploratory_gate"]["blockers"]


def test_different_component_names_do_not_attest_independence():
    report = evaluate_experiment([_gold(1)], [_prediction(1)], threshold_frozen_before_test=True)
    components = report["model_components"][0]
    assert components["distinct_family_labels"] is True
    assert components["independent_families"] is False
    assert components["independence_status"] == "not_attested"
