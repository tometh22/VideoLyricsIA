from auto_repair_policy import candidate_has_independent_evidence


def test_aliases_of_one_recognizer_do_not_satisfy_independence_gate():
    assert not candidate_has_independent_evidence(
        {"source_families": ["WhisperX", "openai_whisper"]},
        "content_reversible",
    )
    assert candidate_has_independent_evidence(
        {"source_families": ["WhisperX", "gemini_audio"]},
        "content_reversible",
    )
