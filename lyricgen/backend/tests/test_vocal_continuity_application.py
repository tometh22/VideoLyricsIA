"""Staging-only application, provenance, and stale/protected-result guards."""

import copy

import pytest
import vocal_continuity as vc


def result():
    return {
        "segments": [
            {
                "start": 1.0,
                "end": 2.0,
                "text": "amor",
                "words": [{"word": "amor", "start": 1.0, "end": 2.0}],
                "provider_evidence": {
                    "words": [{"word": "amor", "start": 1.0, "end": 2.0}]
                },
                "timing_provenance": {"source": "ctc_alignment"},
            },
            {"start": 4.0, "end": 5.0, "text": "otra"},
        ]
    }


def report(value):
    return {
        "status": "shadow",
        "clock": {"verified": True, "offset_s": 0.025},
        "audio_duration_s": 6.0,
        "source_segments_fingerprint": vc._segments_fingerprint(value["segments"]),
        "candidates": [
            {
                "segment_index": 0,
                "current_end": 2.0,
                "proposed_end": 3.0,
                "boundary_spread_s": 0.04,
                "selector_policy": vc.POLICY_VERSION,
                "periodicity_evidence": {
                    "supported": True,
                    "version": vc.PERIODICITY_VERSION,
                    "longest_run_frames": 3,
                },
            }
        ],
    }


@pytest.fixture
def application(monkeypatch):
    monkeypatch.setenv("VOCAL_CONTINUITY_MODE", "apply")
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setattr(vc, "observe_vocal_continuity", lambda value, *a: report(value))


def test_application_preserves_text_words_and_provider_evidence(application):
    original = result()
    frozen = copy.deepcopy(original)
    output = vc.process_vocal_continuity(original, "mix", "stem")
    assert original == frozen
    assert output["segments"][0]["end"] == 3.0
    for key in ["start", "text", "words", "provider_evidence", "timing_provenance"]:
        assert output["segments"][0][key] == original["segments"][0][key]
    assert output["postpass_stats"]["vocal_continuity"]["applied_count"] == 1
    assert output["segments"][0]["vocal_continuity_adjustment"]["original_end"] == 2.0


@pytest.mark.parametrize("environment", ["production", "prod", "", None])
def test_application_cannot_run_in_production_or_unknown_environment(
    monkeypatch, environment
):
    monkeypatch.setenv("VOCAL_CONTINUITY_MODE", "apply")
    if environment is None:
        monkeypatch.delenv("ENVIRONMENT", raising=False)
    else:
        monkeypatch.setenv("ENVIRONMENT", environment)
    monkeypatch.setattr(
        vc, "observe_vocal_continuity", lambda *a: pytest.fail("must not analyze")
    )
    original = result()
    output = vc.process_vocal_continuity(original, "mix", "stem")
    assert output["segments"] == original["segments"]
    assert (
        output["postpass_stats"]["vocal_continuity"]["reason"]
        == "application_environment_not_allowed"
    )


@pytest.mark.parametrize("mode", ["off", "typo", ""])
def test_explicit_off_or_invalid_mode_overrides_shadow_flag(monkeypatch, mode):
    monkeypatch.setenv("VOCAL_CONTINUITY_MODE", mode)
    monkeypatch.setenv("VOCAL_CONTINUITY_SHADOW_ENABLED", "1")
    monkeypatch.setattr(
        vc, "observe_vocal_continuity", lambda *a: pytest.fail("disabled")
    )
    original = result()
    assert vc.process_vocal_continuity(original, "mix", "stem") is original


def test_shadow_does_not_apply_valid_candidates(application, monkeypatch):
    monkeypatch.setenv("VOCAL_CONTINUITY_MODE", "shadow")
    original = result()
    output = vc.process_vocal_continuity(original, "mix", "stem")
    assert output["segments"] == original["segments"]


@pytest.mark.parametrize(
    "protection", ["locked", "operator_locked", "is_approved", "approved_at"]
)
def test_locked_or_approved_lines_are_preserved(application, protection):
    original = result()
    original["segments"][0][protection] = True
    assert (
        vc.process_vocal_continuity(original, "mix", "stem")["segments"]
        == original["segments"]
    )


@pytest.mark.parametrize(
    "protection",
    [
        {"approved_text_reuse": {"used": True}},
        {"is_approved": True},
        {"approved_at": "date"},
        {"operator_locked": True},
    ],
)
def test_approved_or_locked_document_is_preserved(application, protection):
    original = {**result(), **protection}
    assert (
        vc.process_vocal_continuity(original, "mix", "stem")["segments"]
        == original["segments"]
    )


def test_stale_word_or_text_change_rejects_even_when_end_is_unchanged(
    application, monkeypatch
):
    original = result()
    stale = report(original)
    original["segments"][0]["text"] = "changed"
    monkeypatch.setattr(vc, "observe_vocal_continuity", lambda *a: stale)
    output = vc.process_vocal_continuity(original, "mix", "stem")
    assert output["segments"] == original["segments"]
    assert (
        output["postpass_stats"]["vocal_continuity"]["reason"]
        == "stale_or_protected_result"
    )


@pytest.mark.parametrize(
    "change",
    [
        {"proposed_end": 4.1},
        {"proposed_end": 6.0},
        {"proposed_end": 1.8},
        {"proposed_end": float("nan")},
        {"current_end": 1.9},
        {"boundary_spread_s": 0.2},
        {"selector_policy": "old"},
        {"periodicity_evidence": {"supported": False}},
        {
            "periodicity_evidence": {
                "supported": True,
                "version": vc.PERIODICITY_VERSION,
                "longest_run_frames": 1,
            }
        },
        {"segment_index": True},
        {"segment_index": -1},
    ],
)
def test_invalid_or_unscreened_candidate_cannot_mutate(
    application, monkeypatch, change
):
    original = result()
    bad = report(original)
    bad["candidates"][0].update(change)
    monkeypatch.setattr(vc, "observe_vocal_continuity", lambda *a: bad)
    assert (
        vc.process_vocal_continuity(original, "mix", "stem")["segments"]
        == original["segments"]
    )


def test_duplicate_proposals_are_rejected(application, monkeypatch):
    original = result()
    bad = report(original)
    bad["candidates"] *= 2
    monkeypatch.setattr(vc, "observe_vocal_continuity", lambda *a: bad)
    assert (
        vc.process_vocal_continuity(original, "mix", "stem")["segments"]
        == original["segments"]
    )


def test_second_measurement_keeps_audit_and_does_not_decode_again(
    application, monkeypatch
):
    first = vc.process_vocal_continuity(result(), "mix", "stem")
    monkeypatch.setattr(
        vc, "observe_vocal_continuity", lambda *a: pytest.fail("must not rerun")
    )
    assert vc.process_vocal_continuity(first, "mix", "stem") is first


def test_measurement_applies_without_private_word_stream_and_skips_legacy(
    application, monkeypatch, tmp_path
):
    import transcription_worker
    import timing_review_suggestions
    import pipeline
    import vocal_sep

    audio = tmp_path / "audio.wav"
    audio.touch()
    monkeypatch.setenv("STABLE_PITCH_TAIL_ENABLED", "1")
    monkeypatch.setattr(pipeline, "_audio_duration", lambda *a: 6.0)
    monkeypatch.setattr(vocal_sep, "separate_vocals", lambda *a, **kw: None)
    monkeypatch.setattr(
        timing_review_suggestions,
        "load_acoustic_track",
        lambda *a: pytest.fail("legacy must not run"),
    )
    source = result()
    output = transcription_worker._finalize_vocal_continuity(
        source, str(audio), fresh_transcription=True
    )
    assert output["segments"][0]["end"] == 3.0
    assert source["segments"][0]["end"] == 2.0
    assert "stable_pitch_tail" not in output["postpass_stats"]


def test_finalizer_never_analyzes_existing_document_in_apply_mode(
    application, monkeypatch
):
    import transcription_worker

    monkeypatch.setattr(
        vc, "process_vocal_continuity", lambda *a: pytest.fail("existing document")
    )
    source = result()
    output = transcription_worker._finalize_vocal_continuity(
        source, "not-read.wav", fresh_transcription=False
    )
    assert output["segments"] == source["segments"]
    assert (
        output["postpass_stats"]["vocal_continuity"]["reason"]
        == "fresh_transcription_required"
    )
