import hashlib
import base64
import json
import os
import tempfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import transcription_quality as tq
from evidence_attestation import sign_artifact


def _segment(start, end, text="line"):
    return {"start": start, "end": end, "text": text}


def _calibrate(monkeypatch):
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_CALIBRATED", "1")
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_CALIBRATION_ID", "gold-v5-test")
    monkeypatch.setenv(
        "TRANSCRIPTION_QUALITY_CALIBRATION_POLICY", tq.POLICY_VERSION,
    )
    fingerprint = tq.runtime_identity()["pipeline_config_fingerprint"]
    monkeypatch.setenv(
        "TRANSCRIPTION_QUALITY_CALIBRATION_CONFIG_FINGERPRINT",
        fingerprint,
    )
    manifest = tempfile.NamedTemporaryFile(mode="wb", delete=False)
    manifest_raw = b'{"schema_version":5,"benchmark_id":"test"}'
    manifest.write(manifest_raw)
    manifest.close()
    monkeypatch.setenv(
        "TRANSCRIPTION_QUALITY_BENCHMARK_MANIFEST_PATH", manifest.name,
    )
    report = {
        "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        "systems": {"candidate": {
            "release": tq.runtime_identity()["pipeline_release"],
            "pipeline_config_fingerprint": fingerprint,
        }},
        "release_gate": {
            "decision": "GO",
            "checks": {name: True for name in tq.RELEASE_REPORT_REQUIRED_CHECKS},
        },
    }
    private_raw = hashlib.sha256(b"quality-release-test-key").digest()
    private = Ed25519PrivateKey.from_private_bytes(private_raw)
    public_raw = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    monkeypatch.setenv("BENCHMARK_RELEASE_PUBLIC_KEYS", json.dumps({
        "test-release": base64.b64encode(public_raw).decode("ascii"),
    }))
    report = sign_artifact(
        report, base64.b64encode(private_raw).decode("ascii"), "test-release",
    )
    handle = tempfile.NamedTemporaryFile(mode="wb", delete=False)
    raw = json.dumps(report, sort_keys=True).encode("utf-8")
    handle.write(raw)
    handle.close()
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_RELEASE_REPORT_PATH", handle.name)
    monkeypatch.setenv(
        "TRANSCRIPTION_QUALITY_RELEASE_REPORT_SHA256",
        hashlib.sha256(raw).hexdigest(),
    )


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
    segments = [_segment(1, 2)]
    quality = tq.evaluate(segments, {
        "audio_coverage": 0.5, "text_mismatches": 0,
        "voiced_gap_s": 0, "uncovered_seconds": 0,
    })
    content_hash = tq.segments_hash(segments)
    quality["evaluated_revision"] = 4
    quality["acknowledgement"] = {
        "revision": 4,
        "segments_hash": content_hash,
        "policy_version": tq.POLICY_VERSION,
        "confirmed_window_ids": [],
        "quality_fingerprint": tq.quality_fingerprint(
            quality, revision=4, content_hash=content_hash,
        ),
    }
    assert tq.can_render(quality, revision=4, segments=segments)[0] is True
    assert tq.can_render(quality, revision=5, segments=segments)[0] is False


def test_runtime_observe_is_authoritative_kill_switch(monkeypatch):
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_MODE", "enforce")
    segments = [_segment(1, 2)]
    quality = tq.evaluate(segments, {
        "audio_coverage": 0.1, "text_mismatches": 1,
        "voiced_gap_s": 1, "uncovered_seconds": 1,
    })
    quality["evaluated_revision"] = 0
    assert quality["mode"] == "enforce"
    assert tq.can_render(quality, revision=0, segments=segments)[0] is False

    monkeypatch.setenv("TRANSCRIPTION_QUALITY_MODE", "observe")
    assert tq.can_render(quality, revision=0, segments=segments) == (True, None)


def test_runtime_enforce_rejects_stale_observe_verdict(monkeypatch):
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_MODE", "observe")
    segments = [_segment(1, 2)]
    quality = tq.evaluate(segments, {
        "audio_coverage": 1.0, "text_mismatches": 0,
        "voiced_gap_s": 0, "uncovered_seconds": 0,
    })
    quality["evaluated_revision"] = 0
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_MODE", "enforce")
    assert tq.can_render(quality, revision=0, segments=segments)[0] is False


def test_quality_fingerprint_changes_with_acoustic_evidence(monkeypatch):
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_MODE", "enforce")
    segments = [_segment(1, 2)]
    quality = tq.evaluate(segments, {
        "audio_coverage": 0.5, "text_mismatches": 0,
        "voiced_gap_s": 0, "uncovered_seconds": 0,
    }, acoustic_evidence={"windows": [{"event_count": 1}]})
    quality["evaluated_revision"] = 3
    content_hash = tq.segments_hash(segments)
    original = tq.quality_fingerprint(
        quality, revision=3, content_hash=content_hash,
    )
    quality["acknowledgement"] = {
        "revision": 3, "segments_hash": content_hash,
        "policy_version": tq.POLICY_VERSION,
        "confirmed_window_ids": [], "quality_fingerprint": original,
    }
    quality["acoustic_evidence"] = {"windows": [{"event_count": 2}]}
    assert tq.quality_fingerprint(
        quality, revision=3, content_hash=content_hash,
    ) != original
    assert tq.can_render(quality, revision=3, segments=segments)[0] is False


def test_pass_verdict_is_bound_to_revision_and_exact_content(monkeypatch):
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_MODE", "enforce")
    _calibrate(monkeypatch)
    segments = [_segment(1, 2, "original")]
    quality = tq.evaluate(segments, {
        "audio_coverage": 1.0, "text_mismatches": 0,
        "voiced_gap_s": 0, "uncovered_seconds": 0,
    })
    quality["evaluated_revision"] = 7
    quality["quality_fingerprint"] = tq.quality_fingerprint(
        quality, revision=7, content_hash=tq.segments_hash(segments),
    )

    assert tq.can_render(quality, revision=7, segments=segments)[0] is True
    assert tq.can_render(quality, revision=8, segments=segments)[0] is False
    changed = [_segment(1, 2, "changed after evaluation")]
    assert tq.can_render(quality, revision=7, segments=changed)[0] is False


def test_enforcement_rollout_is_job_and_tenant_scoped(monkeypatch):
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_MODE", "enforce")
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_ENFORCE_PERCENT", "0")
    monkeypatch.setenv(
        "TRANSCRIPTION_QUALITY_ENFORCE_PILOT_TENANTS", "pilot-tenant",
    )
    assert tq.effective_policy_mode(
        job_id="regular-job", tenant_id="regular-tenant",
    ) == "observe"
    assert tq.effective_policy_mode(
        job_id="pilot-job", tenant_id="pilot-tenant",
    ) == "enforce"
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_ENFORCE_PERCENT", "100")
    assert tq.effective_policy_mode(
        job_id="regular-job", tenant_id="regular-tenant",
    ) == "enforce"


def test_zero_duration_and_destructive_overlap_are_critical(monkeypatch):
    _calibrate(monkeypatch)
    evidence = {
        "audio_coverage": 1.0, "text_mismatches": 0,
        "voiced_gap_s": 0, "uncovered_seconds": 0,
    }
    zero = tq.evaluate([_segment(1, 1)], evidence)
    assert zero["decision"] == "review_required"
    assert zero["metrics"]["invalid_ranges"] == 1
    overlap = tq.evaluate(
        [_segment(1, 10), _segment(2, 3)], evidence,
    )
    assert overlap["decision"] == "review_required"
    assert any(
        reason["code"] == "severe_line_overlaps"
        and reason["severity"] == "critical"
        for reason in overlap["reasons"]
    )


def test_pass_rejects_mutated_evidence_with_stale_fingerprint(monkeypatch):
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_MODE", "enforce")
    _calibrate(monkeypatch)
    segments = [_segment(1, 2)]
    quality = tq.evaluate(segments, {
        "audio_coverage": 1.0, "text_mismatches": 0,
        "voiced_gap_s": 0, "uncovered_seconds": 0,
    })
    quality["evaluated_revision"] = 1
    quality["quality_fingerprint"] = tq.quality_fingerprint(
        quality, revision=1, content_hash=tq.segments_hash(segments),
    )
    quality["acoustic_evidence"] = {"strong_unassigned_events": 99}
    assert tq.can_render(quality, revision=1, segments=segments)[0] is False


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


def test_old_structural_repair_is_pending_operator_review_in_v5():
    segment = {
        **_segment(1, 2, "Real uoo uou"),
        "consensus_reprocessed": True,
        "structural_hybrid": True,
        "review": False,
    }
    quality = tq.evaluate([segment], {
        "audio_coverage": 1.0, "text_mismatches": 0,
        "voiced_gap_s": 0, "uncovered_seconds": 0,
    })
    assert quality["decision"] == "review_required"
    assert any(
        reason["code"] == "structural_autorepair_uncalibrated"
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


def test_live_gate_requires_an_independent_acoustic_witness(monkeypatch):
    _calibrate(monkeypatch)
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


def test_live_structural_disagreement_opens_contextual_retry_window(monkeypatch):
    monkeypatch.setattr("audio_coverage.text_mismatches", lambda *_: [])
    monkeypatch.setattr("audio_coverage.uncovered_spans", lambda *_: [])
    windows = tq.build_unsafe_windows(
        [_segment(62, 67, "Real")], [],
        structural_disagreements=[{
            "index": 0, "start": 62, "end": 67,
            "suggestion": "Real, wow wow",
        }],
    )
    assert len(windows) == 1
    assert windows[0]["start"] == 55.5
    assert windows[0]["end"] == 83.5
    assert windows[0]["reasons"] == ["live_structural_disagreement"]


def test_live_structural_disagreement_blocks_and_triggers_retry_eligibility():
    quality = tq.evaluate(
        [
            _segment(60.85, 63.28, "Real"),
            _segment(63.29, 64.01, "Real"),
            _segment(64.02, 65.10, "Real"),
            _segment(67.03, 68.36, "Real"),
        ],
        {
            "audio_coverage": 1.0, "text_mismatches": 0,
            "voiced_gap_s": 0, "uncovered_seconds": 0,
            "live_structural_disagreements": 4,
        },
        unsafe_windows=[{
            "start": 54.36, "end": 87.01,
            "reasons": ["live_structural_disagreement"],
            "segment_indices": [0, 1, 2, 3],
        }],
    )
    assert quality["decision"] == "review_required"
    assert quality["render_blocked"] is True
    assert quality["score"] is None
    assert quality["risk"] > 0
    assert any(
        reason["code"] == "live_structural_disagreement"
        and reason["value"] == 4
        for reason in quality["reasons"]
    )


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


def test_human_revision_supersedes_and_invalidates_all_stale_evidence():
    pending = {
        "decision": "review_required", "analysis_status": "pending",
        "analysis_pending": True, "analysis_job_id": "quality:old",
        "unsafe_windows": [{
            "id": "qw_1", "start": 10, "end": 20,
            "reasons": ["text_mismatch"],
        }],
        "acknowledgement": {"revision": 0},
        "quality_fingerprint": "old", "acoustic_evidence": {"old": True},
        "analysis_windows": [{"old": True}], "retry": {"old": True},
    }
    updated = tq.supersede_pending_analysis(pending, revision=3)
    assert updated["analysis_pending"] is False
    assert updated["analysis_status"] == "superseded_by_edit"
    assert updated["analysis_superseded_revision"] == 3
    assert updated["unsafe_windows"][0]["start"] == 10
    assert updated["unsafe_windows"][0]["end"] == 20
    assert updated["unsafe_windows"][0]["reasons"] == [
        "superseded_quality_window", "text_mismatch",
    ]
    assert updated["decision"] == "review_required"
    assert updated["render_blocked"] is True
    assert tq.manual_override_allowed(updated) is False
    for key in (
        "acknowledgement", "quality_fingerprint", "acoustic_evidence",
        "analysis_windows", "retry", "analysis_job_id",
    ):
        assert key not in updated
    assert pending["analysis_pending"] is True


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


def test_fingerprint_covers_ctc_and_acoustic_runtime_configuration(monkeypatch):
    original = tq.runtime_identity()["pipeline_config_fingerprint"]
    for key, value in (
        ("CTC_ALIGN_MODEL", "different/model"),
        ("CTC_ALIGN_STAR_DELTA", "9.1"),
        ("TARGETED_CTC_STEM_SCORE_MIN", ".77"),
        ("TARGETED_ACOUSTIC_STEM_DTW_MAX", ".12"),
        ("DEMUCS_VARIANT", "different-stem"),
        ("CTC_ALIGN_MAX_AUDIO_S", "42"),
        ("VOCAL_SEP_ENABLED", "0"),
        ("QUALITY_ASR_USD_PER_MINUTE", ".123"),
    ):
        monkeypatch.setenv(key, value)
        changed = tq.runtime_identity()["pipeline_config_fingerprint"]
        assert changed != original, key
        monkeypatch.delenv(key)


def test_segments_hash_covers_word_karaoke_and_visual_row_fields():
    base = [{
        "start": 1, "end": 2, "text": "line", "scale": 1,
        "pos": [100, 200], "rot": 0,
        "words": [{"start": 1, "end": 1.5, "word": "line"}],
    }]
    original = tq.segments_hash(base)
    for field, value in (
        ("scale", 1.2), ("pos", [101, 200]), ("rot", 2),
        ("words", [{"start": 1.4, "end": 1.9, "word": "line"}]),
    ):
        changed = [{**base[0], field: value}]
        assert tq.segments_hash(changed) != original, field


def test_revoked_release_report_revokes_persisted_pass(monkeypatch):
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_MODE", "enforce")
    _calibrate(monkeypatch)
    report_path = os.environ["TRANSCRIPTION_QUALITY_RELEASE_REPORT_PATH"]
    segments = [_segment(1, 2)]
    quality = tq.evaluate(segments, {
        "audio_coverage": 1.0, "text_mismatches": 0,
        "voiced_gap_s": 0, "uncovered_seconds": 0,
    })
    quality["evaluated_revision"] = 0
    quality["quality_fingerprint"] = tq.quality_fingerprint(
        quality, revision=0, content_hash=tq.segments_hash(segments),
    )
    assert tq.can_render(quality, revision=0, segments=segments)[0] is True
    os.unlink(report_path)
    assert tq.can_render(quality, revision=0, segments=segments)[0] is False


def test_unsigned_or_minimal_go_report_cannot_calibrate(monkeypatch):
    _calibrate(monkeypatch)
    report_path = os.environ["TRANSCRIPTION_QUALITY_RELEASE_REPORT_PATH"]
    unsigned = {
        "manifest_sha256": "a" * 64,
        "systems": {"candidate": {
            "release": tq.runtime_identity()["pipeline_release"],
            "pipeline_config_fingerprint": tq.runtime_identity()[
                "pipeline_config_fingerprint"
            ],
        }},
        "release_gate": {"decision": "GO", "checks": {}},
    }
    raw = json.dumps(unsigned, sort_keys=True).encode("utf-8")
    with open(report_path, "wb") as handle:
        handle.write(raw)
    monkeypatch.setenv(
        "TRANSCRIPTION_QUALITY_RELEASE_REPORT_SHA256",
        hashlib.sha256(raw).hexdigest(),
    )
    assert tq.calibration_identity()["calibrated"] is False


def test_v5_exposes_dimension_risks_and_never_reports_false_100():
    quality = tq.evaluate(
        [_segment(45.9, 48), _segment(45.1, 47)],
        {"audio_coverage": 1.0, "text_mismatches": 0,
         "voiced_gap_s": 0, "uncovered_seconds": 0},
    )
    assert quality["policy_version"] == "lyrics-quality-v5"
    assert quality["risk_dimensions"]["timeline_integrity"] == 1.0
    assert quality["score"] is None
    assert quality["risk_calibration"]["calibrated"] is False


def test_uncalibrated_clean_result_fails_closed_to_review():
    quality = tq.evaluate(
        [_segment(1, 2)],
        {"audio_coverage": 1.0, "text_mismatches": 0,
         "voiced_gap_s": 0, "uncovered_seconds": 0},
    )
    assert quality["decision"] == "review_required"
    assert quality["score"] is None
    assert any(
        reason["code"] == "quality_calibration_unavailable"
        for reason in quality["reasons"]
    )


def test_acknowledgement_is_bound_to_exact_quality_evidence(monkeypatch):
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_MODE", "enforce")
    segments = [_segment(1, 2)]
    first_window = tq._merge_windows([
        {"start": 10, "end": 12, "reason": "text_mismatch"},
    ], pad_s=0)
    quality = tq.evaluate(
        segments,
        {"audio_coverage": .9, "text_mismatches": 1,
         "voiced_gap_s": 0, "uncovered_seconds": 0},
        unsafe_windows=first_window,
    )
    content_hash = tq.segments_hash(segments)
    quality["evaluated_revision"] = 2
    quality["acknowledgement"] = {
        "revision": 2, "segments_hash": content_hash,
        "policy_version": tq.POLICY_VERSION,
        "confirmed_window_ids": [first_window[0]["id"]],
        "quality_fingerprint": tq.quality_fingerprint(
            quality, revision=2, content_hash=content_hash,
        ),
    }
    assert tq.can_render(quality, revision=2, segments=segments)[0] is True

    quality["unsafe_windows"] = tq._merge_windows([
        {"start": 20, "end": 24, "reason": "event_count"},
    ], pad_s=0)
    assert tq.can_render(quality, revision=2, segments=segments)[0] is False


def test_fatal_timeline_failure_cannot_be_manually_overridden(monkeypatch):
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_MODE", "enforce")
    segments = [_segment(4, 5), _segment(3, 4)]
    quality = tq.evaluate(
        segments,
        {"audio_coverage": 1.0, "text_mismatches": 0,
         "voiced_gap_s": 0, "uncovered_seconds": 0},
    )
    assert tq.manual_override_allowed(quality) is False


def test_failed_isolated_retry_is_explicit_and_keeps_review_gate():
    quality = tq.evaluate(
        [_segment(1, 2)],
        {"audio_coverage": 1.0, "text_mismatches": 0,
         "voiced_gap_s": 0, "uncovered_seconds": 0},
        unsafe_windows=[{"start": 1, "end": 2, "reasons": ["text_mismatch"]}],
        retry_stats={"attempted": True, "failed": True,
                     "failure_reason": "provider_timeout"},
    )
    assert quality["decision"] == "retry_failed"
    assert quality["render_blocked"] is True
    assert quality["retry"]["failure_reason"] == "provider_timeout"


def test_v5_window_confirmation_requires_exact_stable_ids():
    windows = tq._merge_windows([
        {"start": 43, "end": 52, "reason": "text_mismatch"},
        {"start": 60, "end": 83, "reason": "event_count"},
    ], pad_s=0)
    quality = {"unsafe_windows": windows}
    ids = [window["id"] for window in windows]
    assert tq.confirmed_all_windows(quality, ids) is True
    assert tq.confirmed_all_windows(quality, ids[:1]) is False
    assert tq.confirmed_all_windows(quality, [*ids, "made-up"]) is False
