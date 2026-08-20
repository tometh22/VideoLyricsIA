"""Regression tests for the canonical job-state contract."""

from job_states import (
    FAILURE_TERMINAL_STATUSES,
    SUCCESS_TERMINAL_STATUSES,
    TERMINAL_STATUSES,
    can_background_transition,
)


def test_terminal_partitions_are_complete_and_disjoint():
    assert SUCCESS_TERMINAL_STATUSES.isdisjoint(FAILURE_TERMINAL_STATUSES)
    assert SUCCESS_TERMINAL_STATUSES | FAILURE_TERMINAL_STATUSES == TERMINAL_STATUSES
    assert "pending_review" in TERMINAL_STATUSES
    assert "transcription_failed" in TERMINAL_STATUSES


def test_background_writer_cannot_resurrect_any_terminal_state():
    for current in TERMINAL_STATUSES:
        for target in ("queued", "processing", "editing", "transcribing"):
            assert not can_background_transition(current, target), (current, target)


def test_terminal_updates_and_field_only_updates_remain_allowed():
    assert can_background_transition("processing", "error")
    assert can_background_transition("done", "done")
    assert can_background_transition("pending_review", None)
