from reference_hypothesis import (
    audio_only_batch_mode,
    build,
    build_from_candidate,
    build_unavailable,
    recover_from_machine_evidence,
    validate_binding,
)
from machine_evidence import build_machine_evidence, finalize_machine_evidence


def test_reference_hypothesis_is_audio_bound_and_memory_completion_forbidden():
    hypothesis = build(
        text="one\n two ",
        provider="gemini-2.5-flash-audio",
        audio_sha256="a" * 64,
        audio_revision=3,
        source_kind="gemini_complete_audio_derived",
        complete_audio_verified=True,
    )
    assert hypothesis["line_count"] == 2
    assert hypothesis["verification"]["memory_completion_prohibited"] is True
    assert validate_binding(
        hypothesis, audio_sha256="a" * 64, audio_revision=3,
    ) == (True, "ok")
    assert validate_binding(
        hypothesis, audio_sha256="b" * 64, audio_revision=3,
    ) == (False, "reference_audio_mismatch")


def test_unavailable_hypothesis_is_valid_only_for_the_exact_audio():
    hypothesis = build_unavailable(
        provider="gemini-2.5-flash-audio",
        audio_sha256="c" * 64,
        audio_revision=4,
    )
    assert hypothesis["availability"] == "unavailable"
    assert hypothesis["reference_text"] == ""
    assert hypothesis["review_status"] == "manual_full_review_required"
    assert validate_binding(
        hypothesis, audio_sha256="c" * 64, audio_revision=4,
    ) == (True, "ok")
    assert validate_binding(
        hypothesis, audio_sha256="c" * 64, audio_revision=5,
    ) == (False, "reference_audio_revision_mismatch")


def test_external_lyrics_are_disabled_only_for_required_batch_ingestion():
    assert audio_only_batch_mode(reference_required=True, workload_class="batch")
    assert not audio_only_batch_mode(
        reference_required=False, workload_class="batch",
    )
    assert not audio_only_batch_mode(
        reference_required=True, workload_class="interactive",
    )


def test_missing_gemini_candidate_continues_as_manual_review_marker():
    hypothesis, manual = build_from_candidate(
        {}, fallback_text="", audio_sha256="d" * 64, audio_revision=2,
    )
    assert manual is True
    assert hypothesis["availability"] == "unavailable"
    assert hypothesis["review_status"] == "manual_full_review_required"


def _recoverable_machine_evidence(*, audio_sha256="e" * 64, audio_revision=2):
    segments = [{"segment_id": "line-1", "start": 0, "end": 1, "text": "Hola"}]
    quality = {
        "pipeline_release": "release-sha",
        "pipeline_config_fingerprint": "config-sha",
        "reference_attestation": {"text_status": "audio_attested"},
    }
    captured = build_machine_evidence({
        "segments": segments,
        "_recognition_attempt_count": 1,
        "_recognition_hypotheses": [{
            "family": "google/gemini-2.5-flash-audio",
            "kind": "text",
            "events": [{"text": "Hola\nmundo"}],
            "attempt_id": 0,
            "view": "full_audio_with_reference",
            "transformation": "gemini_cleanup_raw",
        }],
    })
    return segments, finalize_machine_evidence(
        captured,
        original_segments=segments,
        quality=quality,
        audio_sha256=audio_sha256,
        audio_revision=audio_revision,
    )


def test_missing_reference_recovers_only_from_valid_audio_bound_machine_evidence():
    segments, evidence = _recoverable_machine_evidence()
    hypothesis, reason = recover_from_machine_evidence(
        evidence,
        original_segments=segments,
        audio_sha256="e" * 64,
        audio_revision=2,
    )
    assert reason == "ok"
    assert hypothesis["reference_text"] == "Hola\nmundo"
    assert hypothesis["source"]["version"]["recovered_from"] == "machine_evidence"
    assert validate_binding(
        hypothesis, audio_sha256="e" * 64, audio_revision=2,
    ) == (True, "ok")


def test_missing_reference_recovery_refuses_tampered_or_wrong_audio_evidence():
    segments, evidence = _recoverable_machine_evidence()
    tampered = dict(evidence)
    tampered["hypotheses_by_family"] = [
        dict(row) for row in evidence["hypotheses_by_family"]
    ]
    tampered["hypotheses_by_family"][0]["events"] = [{"text": "texto alterado"}]
    assert recover_from_machine_evidence(
        tampered,
        original_segments=segments,
        audio_sha256="e" * 64,
        audio_revision=2,
    )[0] is None
    assert recover_from_machine_evidence(
        evidence,
        original_segments=segments,
        audio_sha256="f" * 64,
        audio_revision=2,
    ) == (None, "reference_recovery_audio_mismatch")
