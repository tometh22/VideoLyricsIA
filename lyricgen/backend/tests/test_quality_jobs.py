import quality_jobs
import transcription_quality as tq


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


def test_calibrated_mapping_resolves_only_when_it_confirms_persisted_rows():
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
    )
    assert unresolved == []
    assert [item["id"] for item in resolved] == ["refrain"]

    diagnostic[0]["evidence"]["content_mapping"]["events"][1]["text"] = "No"
    unresolved, resolved = quality_jobs._confirmed_windows(
        segments, windows, diagnostic,
    )
    assert [item["id"] for item in unresolved] == ["refrain"]
    assert resolved == []
