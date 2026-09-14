import json
from types import SimpleNamespace

from delivery_media_qc import inspect_delivery_media
from delivery_ocr import compare_ocr_observations
from delivery_qc_runtime import (
    approval_gate, build_runtime_report, mark_delivery_qc_stale,
    refresh_check_results,
)


def _probe_runner(payload):
    def run(*_args, **_kwargs):
        return SimpleNamespace(stdout=json.dumps(payload), stderr="", returncode=0)
    return run


def test_media_qc_blocks_missing_audio_and_duration_mismatch(tmp_path):
    asset = tmp_path / "video.mp4"
    asset.write_bytes(b"encoded")
    result = inspect_delivery_media(
        str(asset), expected_duration=10,
        runner=_probe_runner({
            "format": {"duration": "8.5"},
            "streams": [{
                "codec_type": "video", "codec_name": "h264", "width": 1920,
                "height": 1080, "avg_frame_rate": "30/1", "pix_fmt": "yuv420p",
            }],
        }),
    )
    assert {row["code"] for row in result["issues"]} == {
        "MEDIA_AUDIO_STREAM_MISSING", "MEDIA_DURATION_MISMATCH",
    }


def test_ocr_compares_pixels_without_silently_fixing_text():
    issues = compare_ocr_observations(
        [{"kind": "lyric", "segment_index": 0, "seconds": 75.8, "text": "JAMAS", "confidence": .99}],
        metadata={"title": "Tu Cárcel"},
        segments=[{"start": 75, "end": 77, "text": "JAMÁS"}],
    )
    assert issues[0]["code"] == "OCR_LYRIC_MISMATCH"
    assert issues[0]["actual"] == "JAMAS"
    issues = compare_ocr_observations(
        [{"kind": "lyric", "segment_index": 0, "seconds": 186.7, "text": "AVENTRUA", "confidence": .99}],
        metadata={}, segments=[{"start": 186, "end": 188, "text": "AVENTURA"}],
    )
    assert issues[0]["code"] == "OCR_LYRIC_MISMATCH"
    assert issues[0]["auto_fixable"] is False


def test_ocr_accepts_artist_plus_title_title_card():
    issues = compare_ocr_observations(
        [{
            "kind": "title", "seconds": 1.0,
            "text": "LOS HUASOS QUINCHEROS El Corralero", "confidence": .99,
        }],
        metadata={"artist": "Los Huasos Quincheros", "title": "El Corralero"},
        segments=[],
    )
    assert issues == []


def test_enforce_blocks_open_findings_but_observe_never_blocks():
    report = {"status": "COMPLETE", "issues": [{"issue_id": "x", "severity": "FAIL", "status": "OPEN"}]}
    assert approval_gate(report, "observe")["can_approve"] is True
    assert approval_gate(report, "enforce")["can_approve"] is False
    report["issues"][0]["status"] = "RESOLVED_MANUAL"
    assert approval_gate(report, "enforce")["can_approve"] is True


def test_enforce_does_not_block_a_non_deterministic_review():
    report = {
        "status": "COMPLETE",
        "issues": [{
            "issue_id": "overlap-1", "severity": "WARN", "status": "OPEN",
            "result_status": "REVIEW", "blocking": False,
        }],
    }
    gate = approval_gate(report, "enforce")
    assert gate == {
        "blocked": False, "can_approve": True,
        "reason": "review_recommended", "issue_ids": ["overlap-1"],
    }


def test_refresh_check_results_marks_signed_manual_check_as_passed():
    report = {
        "decision": "BLOCK",
        "checks": [{
            "check_id": "umg_black_bars", "status": "REVIEW", "blocking": True,
            "issue_ids": ["check-1"],
        }],
        "issues": [{
            "issue_id": "check-1", "status": "RESOLVED_MANUAL",
            "severity": "FAIL", "result_status": "REVIEW",
            "manual_verification_required": True,
        }],
    }
    refreshed = refresh_check_results(report)
    assert refreshed["checks"][0]["status"] == "PASS"
    assert refreshed["checks"][0]["blocking"] is False
    assert refreshed["check_summary"] == {"total": 1, "pass": 1, "fail": 0, "review": 0, "not_run": 0}
    assert refreshed["decision"] == "PASS"


def test_editor_mutation_marks_report_stale():
    stale = mark_delivery_qc_stale({"status": "COMPLETE", "issues": []}, revision=4, reason="edit")
    assert stale["status"] == "STALE"
    assert stale["segments_revision"] == 4


def test_runtime_report_turns_missing_detectors_into_signed_manual_failures(tmp_path, monkeypatch):
    asset = tmp_path / "video.mp4"
    asset.write_bytes(b"encoded")
    monkeypatch.setenv("DELIVERY_QC_MODE", "observe")
    monkeypatch.setattr(
        "delivery_qc_runtime.inspect_delivery_media",
        lambda *_args, **_kwargs: {
            "probe": {"duration": 2.0, "video": {"fps": 30}, "audio_streams": 1},
            "issues": [], "abstentions": [],
        },
    )
    monkeypatch.setattr(
        "delivery_qc_runtime.inspect_rendered_text",
        lambda *_args, **_kwargs: {"observations": [], "issues": [], "abstentions": [{"detector": "final_frame_ocr", "reason": "disabled"}]},
    )
    job = SimpleNamespace(
        artist="Artista", song_title="Tema", filename="tema.wav", umg_spec=None,
        transcription_quality={}, segments_revision=2, edit_count=1,
    )
    report = build_runtime_report(
        job=job, video_path=str(asset),
        segments=[{"start": 0, "end": 2, "text": "Hola"}],
    )
    assert report["status"] == "COMPLETE"
    assert report["mode"] == "observe"
    assert report["render_identity"]["edit_count"] == 1
    assert report["abstentions"] == []
    manual = [
        row for row in report["issues"]
        if row["detector"] == "mandatory_signed_reviewer_checklist"
    ]
    assert len(manual) == 8
    assert all(row["severity"] == "FAIL" and row["status"] == "OPEN" for row in manual)
    assert report["summary"]["fail_count"] == 0
    assert report["summary"]["warn_count"] == 8
    assert any(
        row["detector"] == "final_frame_ocr"
        for row in report["detector_diagnostics"]
    )

    signed_previous = {
        **report,
        "issues": [
            {
                **row,
                "status": "RESOLVED_MANUAL",
                "operator_decision": {"reviewer_name": "Reviewer"},
            }
            for row in report["issues"]
        ],
    }
    rebuilt = build_runtime_report(
        job=job, video_path=str(asset),
        segments=[{"start": 0, "end": 2, "text": "Hola"}],
        previous=signed_previous,
    )
    rebuilt_manual = [
        row for row in rebuilt["issues"]
        if row["detector"] == "mandatory_signed_reviewer_checklist"
    ]
    assert all(row["status"] == "OPEN" for row in rebuilt_manual)


def test_runtime_report_exposes_passed_checks_and_manual_review_state(tmp_path, monkeypatch):
    asset = tmp_path / "video.mp4"
    asset.write_bytes(b"encoded")
    monkeypatch.setenv("DELIVERY_QC_MODE", "enforce")
    monkeypatch.setattr(
        "delivery_qc_runtime.inspect_delivery_media",
        lambda *_args, **_kwargs: {
            "probe": {"duration": 2.0, "video": {"fps": 30}, "audio_streams": 1},
            "issues": [], "abstentions": [],
        },
    )
    monkeypatch.setattr(
        "delivery_qc_runtime.inspect_rendered_text",
        lambda *_args, **_kwargs: {
            "observations": [], "issues": [],
            "abstentions": [{"detector": "final_frame_ocr", "reason": "disabled"}],
        },
    )
    job = SimpleNamespace(
        workload_class="batch", artist="Artista", song_title="Tema", filename="tema.wav",
        umg_spec=None, segments_revision=2, edit_count=0,
        transcription_quality={"decision": "safe"},
    )
    report = build_runtime_report(
        job=job, video_path=str(asset), segments=[{"start": 0, "end": 2, "text": "Hola"}],
    )
    checks = {row["check_id"]: row for row in report["checks"]}
    assert checks["media_container"]["status"] == "PASS"
    assert checks["media_audio"]["status"] == "PASS"
    assert checks["ocr_title"]["status"] == "NOT_RUN"
    assert checks["umg_black_bars"]["status"] == "REVIEW"
    assert checks["umg_black_bars"]["blocking"] is False
    assert report["decision"] == "REVIEW"
    assert report["summary"]["fail_count"] == 0
    assert report["approval"]["reason"] == "review_recommended"


def test_runtime_report_blocks_only_an_objective_detector_failure(tmp_path, monkeypatch):
    asset = tmp_path / "video.mp4"
    asset.write_bytes(b"encoded")
    monkeypatch.setattr(
        "delivery_qc_runtime.inspect_delivery_media",
        lambda *_args, **_kwargs: {
            "probe": {"duration": 2.0, "video": {"fps": 30}, "audio_streams": 0},
            "issues": [{
                "code": "MEDIA_AUDIO_STREAM_MISSING", "severity": "FAIL",
                "summary": "No hay pista de audio", "seconds": [0.0],
            }],
            "abstentions": [],
        },
    )
    monkeypatch.setattr(
        "delivery_qc_runtime.inspect_rendered_text",
        lambda *_args, **_kwargs: {"observations": [], "issues": [], "abstentions": []},
    )
    job = SimpleNamespace(
        workload_class="batch", artist="Artista", song_title="Tema", filename="tema.wav",
        umg_spec=None, segments_revision=1, edit_count=0,
        transcription_quality={"decision": "safe"},
    )
    report = build_runtime_report(
        job=job, video_path=str(asset), segments=[{"start": 0, "end": 2, "text": "Hola"}],
    )
    checks = {row["check_id"]: row for row in report["checks"]}
    assert checks["media_audio"]["status"] == "FAIL"
    assert checks["media_audio"]["blocking"] is True
    assert report["summary"]["fail_count"] == 1
    assert report["approval"]["reason"] == "open_fail"
    assert report["approval"]["can_approve"] is False
