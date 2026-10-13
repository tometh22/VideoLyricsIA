"""Planning logic of scripts/railway_share_pipeline_config.py (pure; no Railway calls)."""
import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "share", Path(__file__).parents[1] / "scripts" / "railway_share_pipeline_config.py")
share = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(share)


def test_identical_values_become_shared_and_referenced_by_every_service():
    raw = {"api": {"A": "1"}, "Worker": {"A": "1"}}
    shared, refs, problems = share.plan(raw, ["A", "UNSET"])
    assert shared == {"A": "1"} and refs == {"api": ["A"], "Worker": ["A"]} and problems == []


def test_conflicting_values_are_never_shared():
    shared, refs, problems = share.plan({"api": {"A": "0.30"}, "Worker": {"A": "0.35"}}, ["A"])
    assert shared == {} and problems and "A" in problems[0]


def test_a_key_set_in_only_some_services_is_a_problem_not_a_silent_choice():
    shared, refs, problems = share.plan({"api": {"A": "1"}, "Worker": {}}, ["A"])
    assert shared == {} and "no todos" in problems[0]


def test_already_shared_keys_are_left_alone_and_the_run_is_idempotent():
    raw = {"api": {"A": "${{shared.A}}"}, "Worker": {"A": "${{shared.A}}"}}
    assert share.plan(raw, ["A"]) == ({}, {"api": [], "Worker": []}, [])


def test_rendered_difference_detection():
    before = {"api": {"A": "1"}}
    assert share.diff_rendered(before, {"api": {"A": "1"}}, ["A"]) == []
    assert share.diff_rendered(before, {"api": {"A": "2"}}, ["A"]) == ["api.A: '1' -> '2'"]


def test_key_list_covers_the_token_inputs_and_timing_config():
    keys = share.config_keys()
    for key in ("CTC_ALIGN_MIN_MED_SCORE", "QUALITY_V6_PROPOSALS_ENABLED", "LYRIC_HOLD_S",
                "TRANSCRIPTION_QUALITY_CALIBRATION_ID"):
        assert key in keys
    assert len(keys) == len(set(keys))
