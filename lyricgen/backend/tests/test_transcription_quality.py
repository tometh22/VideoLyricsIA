import transcription_quality as tq


def _segment(start, end, text="line"):
    return {"start": start, "end": end, "text": text}


def test_gate_blocks_text_audio_mismatch_even_with_full_coverage(monkeypatch):
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_MODE", "enforce")
    quality = tq.evaluate(
        [_segment(10, 13)],
        {
            "audio_coverage": 1.0, "text_mismatches": 1,
            "voiced_gap_s": 0, "uncovered_seconds": 0,
        },
    )
    assert quality["decision"] == "review_required"
    assert quality["render_blocked"] is True
    assert tq.can_render(quality, revision=0) == (
        False, "transcription_quality_review_required"
    )


def test_gate_detects_the_exact_backwards_selector_failure():
    quality = tq.evaluate(
        [_segment(45.9, 48), _segment(45.1, 47)],
        {
            "audio_coverage": 1.0, "text_mismatches": 0,
            "voiced_gap_s": 0, "uncovered_seconds": 0,
        },
    )
    assert quality["metrics"]["start_inversions"] == 1
    assert quality["decision"] == "review_required"


def test_missing_evidence_and_nonfinite_timing_never_pass():
    assert tq.evaluate([_segment(1, 2)], None)["decision"] == "review_required"
    evidence = {
        "audio_coverage": 1.0, "text_mismatches": 0,
        "voiced_gap_s": 0, "uncovered_seconds": 0,
    }
    quality = tq.evaluate([_segment(float("nan"), 2)], evidence)
    assert quality["metrics"]["invalid_ranges"] == 1
    assert quality["decision"] == "review_required"


def test_ack_is_revision_scoped(monkeypatch):
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_MODE", "enforce")
    quality = tq.evaluate([], {
        "audio_coverage": 0.5, "text_mismatches": 0,
        "voiced_gap_s": 0, "uncovered_seconds": 0,
    })
    quality["acknowledgement"] = {
        "revision": 4,
        "segments_hash": tq.segments_hash([]),
        "policy_version": tq.POLICY_VERSION,
    }
    assert tq.can_render(quality, revision=4, segments=[])[0] is True
    assert tq.can_render(quality, revision=5, segments=[])[0] is False


def test_pass_verdict_is_bound_to_revision_and_exact_content(monkeypatch):
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_MODE", "enforce")
    segments = [_segment(1, 2, "original")]
    quality = tq.evaluate(segments, {
        "audio_coverage": 1.0, "text_mismatches": 0,
        "voiced_gap_s": 0, "uncovered_seconds": 0,
    })
    quality["evaluated_revision"] = 7

    assert tq.can_render(quality, revision=7, segments=segments)[0] is True
    assert tq.can_render(quality, revision=8, segments=segments)[0] is False
    changed = [_segment(1, 2, "changed after evaluation")]
    assert tq.can_render(quality, revision=7, segments=changed)[0] is False


def test_old_policy_can_never_authorize_current_render(monkeypatch):
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_MODE", "enforce")
    segments = [_segment(1, 2, "line")]
    quality = tq.evaluate(segments, {
        "audio_coverage": 1.0, "text_mismatches": 0,
        "voiced_gap_s": 0, "uncovered_seconds": 0,
    })
    quality["evaluated_revision"] = 2
    quality["policy_version"] = "old-policy"
    quality["acknowledgement"] = {
        "revision": 2,
        "segments_hash": tq.segments_hash(segments),
        "policy_version": "old-policy",
    }
    assert tq.can_render(quality, revision=2, segments=segments)[0] is False


def test_consensus_insertion_always_requires_operator_review():
    segment = {
        **_segment(1, 2, "candidate"),
        "consensus_reprocessed": True,
        "review": True,
    }
    quality = tq.evaluate([segment], {
        "audio_coverage": 1.0, "text_mismatches": 0,
        "voiced_gap_s": 0, "uncovered_seconds": 0,
    })
    assert quality["decision"] == "review_required"
    assert any(
        reason["code"] == "consensus_insertions_pending_review"
        for reason in quality["reasons"]
    )


def test_unsafe_windows_merge_overlapping_signals(monkeypatch):
    monkeypatch.setattr(
        "audio_coverage.text_mismatches",
        lambda *_: [{"start": 10, "end": 14, "index": 2, "ratio": 0.1}],
    )
    monkeypatch.setattr(
        "audio_coverage.uncovered_spans", lambda *_: [(13, 18, 4)],
    )
    windows = tq.build_unsafe_windows([], [])
    assert len(windows) == 1
    assert windows[0]["segment_indices"] == [2]
    assert set(windows[0]["reasons"]) == {"text_mismatch", "uncovered_asr"}


def test_live_gate_requires_an_independent_acoustic_witness():
    evidence = {
        "audio_coverage": 1.0, "text_mismatches": 0,
        "voiced_gap_s": 0, "uncovered_seconds": 0,
    }
    missing = tq.evaluate(
        [_segment(1, 2)], evidence, require_independent=True,
    )
    assert missing["decision"] == "review_required"
    assert any(
        reason["code"] == "independent_witness_unavailable"
        for reason in missing["reasons"]
    )

    verified = tq.evaluate(
        [_segment(1, 2)], {
            **evidence,
            "independent_witness_words": 20,
            "independent_audio_coverage": 0.95,
            "independent_text_mismatches": 0,
            "independent_uncovered_seconds": 0,
            "audio_duration_s": 60,
        },
        require_independent=True,
    )
    assert verified["decision"] == "pass"


def test_independent_disagreement_blocks_even_when_primary_self_certifies():
    quality = tq.evaluate(
        [_segment(1, 2)], {
            "audio_coverage": 1.0, "text_mismatches": 0,
            "voiced_gap_s": 0, "uncovered_seconds": 0,
            "independent_witness_words": 20,
            "independent_audio_coverage": 0.95,
            "independent_text_mismatches": 1,
            "independent_uncovered_seconds": 0,
            "audio_duration_s": 60,
        },
        require_independent=True,
    )
    assert quality["decision"] == "review_required"
    assert any(
        reason["code"] == "independent_text_audio_mismatch"
        for reason in quality["reasons"]
    )


def test_independent_witness_adds_missing_lyric_windows(monkeypatch):
    monkeypatch.setattr("audio_coverage.text_mismatches", lambda *_: [])
    monkeypatch.setattr(
        "audio_coverage.uncovered_spans",
        lambda _segments, words: [(60, 84, 12)] if words else [],
    )
    windows = tq.build_unsafe_windows(
        [_segment(1, 2)], [], independent_words=[{"word": "Real"}],
    )
    assert len(windows) == 1
    assert windows[0]["reasons"] == ["independent_uncovered_asr"]


def test_unverified_live_lexical_substitution_is_blocking():
    quality = tq.evaluate(
        [_segment(1, 2)], {
            "audio_coverage": 1.0, "text_mismatches": 0,
            "voiced_gap_s": 0, "uncovered_seconds": 0,
            "live_lexical_unverified": 1,
        },
    )
    assert quality["decision"] == "review_required"
    assert any(
        reason["code"] == "live_lexical_unverified"
        for reason in quality["reasons"]
    )


def test_eight_witness_words_cannot_certify_a_five_minute_song():
    quality = tq.evaluate(
        [_segment(1, 2)], {
            "audio_coverage": 1.0, "text_mismatches": 0,
            "voiced_gap_s": 0, "uncovered_seconds": 0,
            "independent_witness_words": 8,
            "independent_audio_coverage": 1.0,
            "independent_text_mismatches": 0,
            "independent_uncovered_seconds": 0,
            "audio_duration_s": 300,
        }, require_independent=True,
    )
    assert any(
        reason["code"] == "independent_witness_too_sparse"
        for reason in quality["reasons"]
    )


def test_pass_from_old_runtime_config_cannot_authorize_render(monkeypatch):
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_MODE", "enforce")
    monkeypatch.setenv("TARGETED_SLOW_STEM_SPEED", "0.88")
    segments = [_segment(1, 2)]
    quality = tq.evaluate(segments, {
        "audio_coverage": 1.0, "text_mismatches": 0,
        "voiced_gap_s": 0, "uncovered_seconds": 0,
    })
    quality["evaluated_revision"] = 0
    monkeypatch.setenv("TARGETED_SLOW_STEM_SPEED", "0.90")
    assert tq.can_render(quality, revision=0, segments=segments)[0] is False
