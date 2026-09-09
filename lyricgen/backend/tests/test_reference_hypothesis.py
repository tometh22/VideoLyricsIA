from reference_hypothesis import (
    audio_only_batch_mode,
    build,
    build_from_candidate,
    build_unavailable,
    validate_binding,
)


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
