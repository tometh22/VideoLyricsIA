"""Generic regression contracts for compressed live outros.

The fixture is sanitized: it contains no artist, title, tenant, job ID or
audio.  The lexical tokens are retained because they are the evidence shape
under test, not production exceptions.  No assertion permits song-specific
branching or a special-case word blacklist.
"""
from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import ctc_align
import line_evidence
import queue_jobs
import transcription_quality as tq


_FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "live_outro_compressed_whisperx.json"
)


@pytest.fixture
def live_outro_fixture() -> dict:
    return json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))


def _coverage(**overrides) -> dict:
    return {
        "audio_coverage": 1.0,
        "text_mismatches": 0,
        "voiced_gap_s": 0.0,
        "uncovered_seconds": 0.0,
        **overrides,
    }


def _window_covering(windows: list[dict], start: float, end: float) -> dict | None:
    return next(
        (
            window for window in windows
            if float(window.get("start", 0.0)) <= start
            and float(window.get("end", 0.0)) >= end
        ),
        None,
    )


def _reason_codes(quality: dict) -> set[str]:
    return {
        str(reason.get("code"))
        for reason in quality.get("reasons") or []
        if isinstance(reason, dict)
    }


def test_sanitized_fixture_preserves_observed_incident_envelopes(
    live_outro_fixture,
):
    raw = live_outro_fixture["whisperx_segments"]
    repeated = [segment for segment in raw if segment["text"] == "Real"]
    assert len(repeated) == 6
    assert repeated[0]["start"] == pytest.approx(60.936)
    assert repeated[-1]["end"] == pytest.approx(68.097)

    tail = next(segment for segment in raw if segment["text"] == "¡Gracias!")
    assert (tail["start"], tail["end"]) == pytest.approx((90.17, 90.731))
    assert tail["words"][0]["score"] == pytest.approx(0.348)

    credit = next(
        segment for segment in raw if segment.get("known_credit_artifact")
    )
    assert credit["start"] == pytest.approx(127.218)


def test_provider_compressed_repetition_emits_structural_issue(
    live_outro_fixture,
):
    repeated = live_outro_fixture["candidate_segments"][0]
    issues = line_evidence.evidence_issues([repeated])

    assert len(issues) == 1
    assert issues[0]["start"] == pytest.approx(60.936)
    assert issues[0]["end"] >= 83.27
    assert issues[0]["end"] <= issues[0]["start"] + 39.0
    assert "provider_timing_collapsed" in set(issues[0]["reasons"])


def test_standalone_tail_lexical_candidate_with_weak_support_emits_issue(
    live_outro_fixture,
):
    repetition, tail = live_outro_fixture["candidate_segments"]
    issues = line_evidence.evidence_issues([repetition, tail])

    tail_issue = next(issue for issue in issues if issue["segment_index"] == 1)
    assert tail_issue["start"] == pytest.approx(96.96)
    assert "isolated_tail_low_support" in set(tail_issue["reasons"])


def test_low_ctc_confidence_is_an_explicit_blocking_reason(live_outro_fixture):
    tail = live_outro_fixture["candidate_segments"][1]

    quality = tq.evaluate([tail], _coverage())

    assert quality["decision"] == "review_required"
    assert quality["shadow_decision"]["would_approve"] is False
    assert "low_ctc_timing_confidence" in _reason_codes(quality)


def test_text_word_cardinality_mismatch_is_not_hidden_by_repeated_token(
    live_outro_fixture,
):
    repeated = live_outro_fixture["candidate_segments"][0]
    assert len(repeated["text"].split()) == 6
    assert len(repeated["words"]) == 1

    issues = line_evidence.evidence_issues([repeated])

    assert len(issues) == 1
    assert "text_word_cardinality_mismatch" in set(issues[0]["reasons"])


def test_stem_mix_disagreement_abstains_instead_of_auto_passing(
    live_outro_fixture,
):
    view = live_outro_fixture["acoustic_views"]
    quality = tq.evaluate(
        live_outro_fixture["candidate_segments"],
        _coverage(),
        acoustic_evidence={
            "windows": [{
                "acoustic_structure": {
                    "accepted": view["accepted"],
                    "reason": view["reason"],
                    "diagnostics": {
                        "stem_event_count": view["stem"]["event_count"],
                        "mix_event_count": view["mix"]["event_count"],
                    },
                },
            }],
        },
    )

    assert quality["decision"] == "review_required"
    assert quality["shadow_decision"]["would_approve"] is False
    assert "acoustic_structure_unavailable" in _reason_codes(quality)


def test_low_confidence_retime_preserves_review_and_lineage(
    live_outro_fixture,
):
    tail = live_outro_fixture["candidate_segments"][1]

    retimed = ctc_align.finalize_line(
        tail,
        96.96,
        97.521,
        [("Gracias", 96.96, 97.521, 0.11)],
        0.11,
        skipped=False,
        recovered=False,
    )

    assert retimed.get("review") is True
    assert set(retimed["review_reasons"]) == set(tail["review_reasons"])
    assert retimed["evidence_lineage"] == tail["evidence_lineage"]
    assert retimed["consensus_sources"] == tail["consensus_sources"]
    assert retimed["ctc_confidence"] == pytest.approx(0.11)


def test_combined_regression_has_two_unsafe_regions_and_never_auto_passes(
    live_outro_fixture,
):
    segments = live_outro_fixture["candidate_segments"]
    raw_words = [
        word
        for segment in live_outro_fixture["whisperx_segments"]
        for word in segment.get("words") or []
    ]
    independent_words = [
        word
        for segment in live_outro_fixture["whisperx_segments"][:6]
        for word in segment.get("words") or []
    ]

    windows = tq.build_unsafe_windows(
        segments, raw_words, independent_words=independent_words,
    )
    repetition = _window_covering(windows, 60.936, 83.27)
    tail = _window_covering(windows, 90.17, 97.521)
    assert repetition is not None, windows
    assert tail is not None, windows

    quality = tq.evaluate(segments, _coverage(), unsafe_windows=windows)
    assert quality["decision"] == "review_required"
    assert quality["render_blocked"] is True
    assert quality["shadow_decision"]["would_approve"] is False


def _called_function_names(function) -> list[str]:
    tree = ast.parse(inspect.getsource(function))
    return [
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]


def test_quality_enqueue_is_wired_from_worker_and_inline_fallback():
    import main
    import transcription_worker

    assert "enqueue_transcription_quality" in _called_function_names(
        transcription_worker.run_transcription_job
    )
    assert "enqueue_transcription_quality" in _called_function_names(
        main._finalize_inline_transcription_quality
    )


def test_quality_enqueue_targets_isolated_queue_with_occ_identity(
    monkeypatch,
):
    captured: dict = {}

    class FakeQueue:
        def __init__(self, name, connection):
            captured["queue_name"] = name
            captured["connection"] = connection

        def enqueue(self, function, **kwargs):
            captured["function"] = function
            captured["enqueue"] = kwargs
            return SimpleNamespace(id=kwargs["job_id"])

    class FakeRetry:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    fake_redis = object()
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_QUEUE_ENABLED", "1")
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_ROLLOUT_PERCENT", "100")
    monkeypatch.setattr(queue_jobs, "_require_submissions_open", lambda: None)
    monkeypatch.setattr(queue_jobs, "_init_redis", lambda: None)
    monkeypatch.setattr(queue_jobs, "_redis", fake_redis)
    monkeypatch.setattr(queue_jobs, "_active_rq_job", lambda *_args: None)
    monkeypatch.setattr(queue_jobs, "_evict_stale_rq_job", lambda *_args: None)
    monkeypatch.setattr(
        queue_jobs, "_mark_transcription_quality_pending",
        lambda *_args: True,
    )

    import rq
    monkeypatch.setattr(rq, "Queue", FakeQueue)
    monkeypatch.setattr(rq, "Retry", FakeRetry)

    content_hash = "a" * 64
    queued_id = queue_jobs.enqueue_transcription_quality(
        "generic-live-job",
        expected_revision=7,
        expected_segments_hash=content_hash,
        filename="sanitized.wav",
        tenant_id="qa-tenant",
    )

    assert captured["queue_name"] == "transcription_quality"
    assert captured["connection"] is fake_redis
    assert queued_id == "transcription-quality:generic-live-job:7:aaaaaaaaaaaa"
    assert captured["enqueue"]["kwargs"] == {
        "expected_revision": 7,
        "expected_segments_hash": content_hash,
        "filename": "sanitized.wav",
    }
    assert captured["enqueue"]["meta"]["expected_revision"] == 7
    assert captured["enqueue"]["meta"]["expected_segments_hash"] == content_hash
