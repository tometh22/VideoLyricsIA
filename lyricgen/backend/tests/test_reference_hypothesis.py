from reference_hypothesis import build, validate_binding


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
