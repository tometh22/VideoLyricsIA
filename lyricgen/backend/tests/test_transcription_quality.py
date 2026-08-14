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
