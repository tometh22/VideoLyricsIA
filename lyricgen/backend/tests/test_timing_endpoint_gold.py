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
    assert test["abstentions"] == 1
    assert report["human_review"]["minutes_per_song"] == {"job-1": 3.5}
    assert report["automatic_apply_allowed"] is False


def test_same_model_family_is_reported_not_misrepresented_as_independent():
    gold = [_gold(1)]
    prediction = _prediction(1)
    prediction["selector_family"] = prediction["proposer_family"]
    report = evaluate_experiment(gold, [prediction], threshold_frozen_before_test=True)
    assert report["model_components"][0]["independent_families"] is False
