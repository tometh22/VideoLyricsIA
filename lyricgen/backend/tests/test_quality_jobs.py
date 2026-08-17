from datetime import datetime, timedelta, timezone
import json
import inspect

import quality_jobs
import structural_hybrid
import transcription_quality as tq


def test_pending_reconciler_filters_and_streams_all_pending_rows():
    source = inspect.getsource(quality_jobs.reconcile_stale_pending_quality_jobs)
    assert '"analysis_pending"' in source
    assert ".order_by(pending_since.asc(), Job.job_id.asc())" in source
    assert ".yield_per(page_size)" in source
    assert ".limit(" not in source


def test_pending_marker_staleness_distinguishes_recent_and_orphaned_jobs():
    now = datetime(2026, 8, 16, tzinfo=timezone.utc)
    recent = {
        "analysis_pending": True,
        "analysis_enqueued_at": (now - timedelta(seconds=30)).isoformat(),
    }
    stale = {
        "analysis_pending": True,
        "analysis_enqueued_at": (now - timedelta(minutes=20)).isoformat(),
    }
    assert quality_jobs._pending_marker_is_stale(
        recent, now=now, max_age_s=900,
    ) is False
    assert quality_jobs._pending_marker_is_stale(
        stale, now=now, max_age_s=900,
    ) is True
    assert quality_jobs._pending_marker_is_stale({
        "analysis_pending": True, "analysis_enqueued_at": "invalid",
    }, now=now) is True


def test_persisted_acoustic_diagnostics_strip_raw_lyrics_and_words():
    secret = "private lyric sentinel"
    sanitized = quality_jobs._sanitize_analytical_evidence({
        "events": [{
            "id": "ae1", "start": 1.0, "end": 2.0,
            "text": secret, "words": [{"word": secret}],
        }],
        "phonetic_candidates": [{"texts": [secret, secret]}],
    })
    encoded = json.dumps(sanitized)
    assert secret not in encoded
    assert sanitized["events"][0]["text_present"] is True
    assert sanitized["events"][0]["word_count"] == 1
    assert sanitized["phonetic_candidates"][0]["text_count"] == 2


def test_quality_windows_are_prioritized_by_risk_then_duration():
    windows = [
        {"id": "early-low", "start": 1, "end": 30,
         "reasons": ["uncovered_asr"]},
        {"id": "late-critical", "start": 60, "end": 64,
         "reasons": ["live_structural_disagreement"]},
        {"id": "early-critical", "start": 40, "end": 42,
         "reasons": ["live_structural_disagreement"]},
    ]
    ordered = quality_jobs._prioritize_windows(windows)
    assert [item["id"] for item in ordered] == [
        "late-critical", "early-critical", "early-low",
    ]


def test_unprocessed_quality_windows_fail_closed():
    quality = tq.evaluate(
        [{"start": 1.0, "end": 2.0, "text": "line"}],
        {"audio_coverage": 1.0, "text_mismatches": 0,
         "voiced_gap_s": 0, "uncovered_seconds": 0},
        retry_stats={"attempted": True, "windows_skipped": 2},
    )
    assert quality["decision"] == "review_required"
    assert any(
        reason["code"] == "quality_windows_unprocessed"
        for reason in quality["reasons"]
    )


def test_truncated_quality_windows_fail_closed():
    quality = tq.evaluate(
        [{"start": 1.0, "end": 2.0, "text": "line"}],
        {"audio_coverage": 1.0, "text_mismatches": 0,
         "voiced_gap_s": 0, "uncovered_seconds": 0},
        retry_stats={"attempted": True, "windows_truncated": 1},
    )
    assert quality["decision"] == "review_required"
    assert any(
        reason["code"] == "quality_windows_truncated"
        for reason in quality["reasons"]
    )


def test_failure_callback_does_not_publish_before_rq_retry(monkeypatch):
    class FakeJob:
        id = "quality:test"
        retries_left = 1
        args = ("job-id",)
        kwargs = {"expected_revision": 0, "expected_segments_hash": "hash"}

    snapshot = __import__("unittest.mock").mock.Mock()
    monkeypatch.setattr(quality_jobs, "_snapshot", snapshot)
    quality_jobs.transcription_quality_failure_callback(
        FakeJob(), None, RuntimeError, RuntimeError("transient"), None,
    )
    snapshot.assert_not_called()


def test_ctc_mapping_cannot_resolve_content_without_independent_attestation(
        monkeypatch):
    stem_hash = "a" * 64
    mix_hash = "b" * 64
    segments = [
        {"start": 60.85, "end": 63.77, "text": "Real, uoh uoh"},
        {"start": 63.77, "end": 67.04, "text": "Real, uoh uoh"},
    ]
    windows = [{
        "id": "refrain", "start": 60.0, "end": 68.0,
        "reasons": ["live_structural_disagreement"],
    }]
    diagnostic = [{
        "window": [57.0, 71.0],
        "evidence": {"content_mapping": {
            "accepted": True, "phonetic_verified": True,
            "selected_candidate_id": "gemini-1",
            "phonetic_evidence": {
                "accepted": True, "schema": "ctc-phonetic-evidence-v1",
                "calibration_id": "gold-v1", "evidence_sha256": "e" * 64,
                "model_identity": {"model_revision": "f" * 40},
            },
            "strong_unassigned_events": 0,
            "events": [dict(item) for item in segments],
        }},
    }]
    unresolved, resolved = quality_jobs._confirmed_windows(
        segments, windows, diagnostic,
        expected_stem_sha256=stem_hash, expected_mix_sha256=mix_hash,
    )
    assert [item["id"] for item in unresolved] == ["refrain"]
    assert resolved == []

    mapping = diagnostic[0]["evidence"]["content_mapping"]
    monkeypatch.setenv("QUALITY_CONTENT_ATTESTATION_KEY", "test-signing-key")
    for event in mapping["events"]:
        event["content_source"] = "gemini_audio"
    attested = structural_hybrid._attest_independent_content(
        mapping, [{
            "source": "slowed_stem_whisper", "family": "openai_whisper",
            "events": [dict(item) for item in segments],
        }], context={
            "window": [57.0, 71.0],
            "stem_sha256": stem_hash, "mix_sha256": mix_hash,
        },
    )
    mapping["independent_content_verified"] = True
    mapping["independent_content_evidence_sha256"] = attested["evidence_sha256"]
    mapping["independent_content_attestation"] = attested["attestation"]
    unresolved, resolved = quality_jobs._confirmed_windows(
        segments, windows, diagnostic,
        expected_stem_sha256=stem_hash, expected_mix_sha256=mix_hash,
    )
    assert unresolved == []
    assert [item["id"] for item in resolved] == ["refrain"]

    original_signature = mapping["independent_content_attestation"][
        "signature_hmac_sha256"
    ]
    mapping["independent_content_attestation"]["signature_hmac_sha256"] = "0" * 64
    unresolved, resolved = quality_jobs._confirmed_windows(
        segments, windows, diagnostic,
        expected_stem_sha256=stem_hash, expected_mix_sha256=mix_hash,
    )
    assert [item["id"] for item in unresolved] == ["refrain"]
    assert resolved == []
    mapping["independent_content_attestation"][
        "signature_hmac_sha256"
    ] = original_signature

    diagnostic[0]["evidence"]["content_mapping"]["events"][1]["text"] = "No"
    unresolved, resolved = quality_jobs._confirmed_windows(
        segments, windows, diagnostic,
        expected_stem_sha256=stem_hash, expected_mix_sha256=mix_hash,
    )
    assert [item["id"] for item in unresolved] == ["refrain"]
    assert resolved == []


def test_independent_attestation_is_bound_to_candidate_window_and_audio(monkeypatch):
    monkeypatch.setenv("QUALITY_CONTENT_ATTESTATION_KEY", "test-signing-key")
    events = [{
        "start": 60.0, "end": 63.0, "text": "Real uoh uoh",
        "content_source": "gemini_audio",
    }]
    mapping = {"selected_candidate_id": "gemini-1", "events": events}
    attested = structural_hybrid._attest_independent_content(
        mapping, [{
            "family": "openai_whisper", "events": [dict(events[0])],
        }], context={
            "window": [57.0, 71.0],
            "stem_sha256": "a" * 64, "mix_sha256": "b" * 64,
        },
    )
    mapping.update({
        "independent_content_attestation": attested["attestation"],
        "independent_content_evidence_sha256": attested["evidence_sha256"],
    })
    valid = lambda **overrides: quality_jobs._valid_independent_content_attestation(
        mapping,
        expected_window=overrides.get("window", [57.0, 71.0]),
        expected_stem_sha256=overrides.get("stem", "a" * 64),
        expected_mix_sha256=overrides.get("mix", "b" * 64),
    )
    assert valid()
    assert not valid(window=[58.0, 71.0])
    assert not valid(stem="c" * 64)
    assert not valid(mix="d" * 64)
    mapping["selected_candidate_id"] = "other-candidate"
    assert not valid()
