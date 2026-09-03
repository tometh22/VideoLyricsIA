import json
from types import SimpleNamespace

from delivery_media_qc import inspect_delivery_media
from delivery_ocr import compare_ocr_observations
from delivery_qc_runtime import approval_gate, build_runtime_report, mark_delivery_qc_stale


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


def test_enforce_blocks_open_findings_but_observe_never_blocks():
    report = {"status": "COMPLETE", "issues": [{"issue_id": "x", "severity": "FAIL", "status": "OPEN"}]}
    assert approval_gate(report, "observe")["can_approve"] is True
    assert approval_gate(report, "enforce")["can_approve"] is False
    report["issues"][0]["status"] = "RESOLVED_MANUAL"
    assert approval_gate(report, "enforce")["can_approve"] is True


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
