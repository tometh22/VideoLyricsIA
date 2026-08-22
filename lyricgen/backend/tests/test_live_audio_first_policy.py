"""Regression guards for live uploads using acoustic ASR as segment truth."""

import inspect

import main


def test_live_flag_reaches_transcription_core():
    params = inspect.signature(main._run_transcription_for_job).parameters
    assert "live" in params
    assert params["live"].default is False


def test_live_audio_truth_bypasses_catalogue_reconciliation():
    src = inspect.getsource(main._run_transcription_for_job)
    truth = src.index("_live_audio_truth = bool(")
    branch = src.index("if _live_audio_truth:", truth)
    reconcile = src.index("_reconciled = _wxr.reconcile", branch)
    assert branch < reconcile
    assert "emitting clean whisperX" in src[branch:reconcile]
    assert "return _emit_segments(" in src[branch:reconcile]


def test_live_checkbox_can_mark_audio_without_filename_marker():
    src = inspect.getsource(main._run_transcription_for_job)
    assert "(live or _looks_live(title, filename))" in src
