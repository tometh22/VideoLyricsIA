import json
from types import SimpleNamespace

import pytest

from delivery_media_qc import inspect_delivery_media
from delivery_ocr import compare_ocr_observations
from delivery_qc_runtime import (
    MANUAL_ATTESTATION_CODE, MANUAL_ATTESTATION_DESCRIPTION,
    MANUAL_ATTESTATION_SUMMARY, MANDATORY_REVIEW_CHECKS, approval_gate, build_runtime_report,
    delivery_qc_source_fingerprint, delivery_qc_visual_fingerprint,
    delivery_readiness_gate, mark_delivery_qc_stale, segments_hash,
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
    assert approval_gate(report, "enforce")["can_approve"] is False
    assert approval_gate(report, "enforce")["reason"] == "open_fail"


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


def test_legacy_manual_reviewer_checks_do_not_block_approval():
    report = {
        "status": "COMPLETE",
        "issues": [{
            "issue_id": "legacy-black-bars",
            "code": "UMG_BLACK_BARS",
            "severity": "FAIL",
            "status": "OPEN",
        }],
    }
    gate = approval_gate(report, "enforce")
    assert gate["blocked"] is False
    assert gate["can_approve"] is True
    assert gate["reason"] == "review_recommended"


def test_legacy_eight_item_checklist_does_not_block_direct_umg_delivery():
    job = SimpleNamespace(
        delivery_profile="umg", workload_class="interactive", status="done",
        job_id="qc-test", editing_started_at=None, input_audio_sha256="a" * 64,
        input_r2_key="input/test.mp3", artist="Test", song_title="Song",
        style="oscuro", render_params={}, scene_plan={}, bg_r2_key_cached=None,
        umg_spec={"frame_size": "HD"}, segments_revision=1,
        segments_json=[{"start": 0, "end": 1, "text": "Hola"}], edit_count=0,
    )
    report = _current_umg_report(job)
    report["issues"] = [{
        "issue_id": f"legacy-{code}", "code": code, "status": "OPEN",
        "severity": "FAIL", "manual_verification_required": True,
    } for code, _summary, _description in MANDATORY_REVIEW_CHECKS]
    assert delivery_readiness_gate(job, report, for_umg_delivery=True)["can_approve"] is True


def _current_umg_report(job, *, attested=True):
    now = "2026-09-27T12:00:00+00:00"
    issues = [{
        "issue_id": "manual-final-review", "code": MANUAL_ATTESTATION_CODE,
        "summary": MANUAL_ATTESTATION_SUMMARY,
        "description": MANUAL_ATTESTATION_DESCRIPTION,
        "status": "RESOLVED_MANUAL" if attested else "OPEN",
        "result_status": "REVIEW", "severity": "WARN",
        "manual_verification_required": True,
        "operator_decision": ({
            "decision": "resolved_manual", "user_id": 12,
            "decided_at": now,
        } if attested else None),
    }]
    return {
        "status": "COMPLETE", "generated_at": now,
        "segments_revision": job.segments_revision,
        "segments_hash": segments_hash(job.segments_json),
        "delivery_spec": dict(job.umg_spec),
        "source_fingerprint": delivery_qc_source_fingerprint(job),
        "visual_fingerprint": delivery_qc_visual_fingerprint(job),
        "render_identity": {"edit_count": job.edit_count},
        "issues": issues,
    }


def test_umg_readiness_requires_current_report_and_one_signed_manual_review():
    job = SimpleNamespace(
        delivery_profile="umg", workload_class="interactive",
        status="done", job_id="qc-test", editing_started_at=None,
        input_audio_sha256="a" * 64, input_r2_key="input/test.mp3",
        artist="Test", song_title="Song", style="oscuro",
        render_params={}, scene_plan={}, bg_r2_key_cached=None,
        umg_spec={"frame_size": "HD", "fps": 29.97},
        segments_revision=3,
        segments_json=[{"start": 0, "end": 2, "text": "Hola"}],
        edit_count=2,
    )
    assert delivery_readiness_gate(job, None, for_umg_delivery=True)["reason"] == "fresh_preflight_required"

    report = _current_umg_report(job, attested=False)
    gate = delivery_readiness_gate(job, report, for_umg_delivery=True)
    assert gate["blocked"] is True
    assert gate["reason"] == "manual_review_required"
    assert gate["issue_ids"] == ["manual-final-review"]

    report = _current_umg_report(job)
    assert delivery_readiness_gate(job, report, for_umg_delivery=True)["can_approve"] is True

    job.edit_count += 1
    assert delivery_readiness_gate(job, report, for_umg_delivery=True)["reason"] == "fresh_preflight_required"

    job.edit_count -= 1
    job.scene_plan = {"scenes": [{"recurrence_key": "chorus", "prompt": "nuevo"}]}
    assert delivery_readiness_gate(job, report, for_umg_delivery=True)["reason"] == "fresh_preflight_required"


def test_blocking_automatic_fail_cannot_be_dismissed_by_closed_status():
    report = {
        "status": "COMPLETE",
        "issues": [{
            "issue_id": "objective-black-frame", "code": "MEDIA_BLACK_FRAME",
            "severity": "FAIL", "result_status": "FAIL", "blocking": True,
            "status": "RESOLVED_MANUAL",
        }],
    }
    gate = approval_gate(report, "enforce")
    assert gate["blocked"] is True
    assert gate["reason"] == "open_fail"
    assert gate["issue_ids"] == ["objective-black-frame"]


def test_manual_signoff_survives_prores_spec_change_but_not_new_visual_cut():
    from delivery_qc_runtime import _merge_prior_decisions

    job = SimpleNamespace(
        delivery_profile="umg", job_id="qc-test", status="done",
        editing_started_at=None, input_audio_sha256="a" * 64,
        input_r2_key="input/test.mp3", artist="Test", song_title="Song",
        style="oscuro", render_params={}, scene_plan={},
        bg_r2_key_cached=None, umg_spec={"frame_size": "HD"},
        segments_revision=1, segments_json=[{"start": 0, "end": 1, "text": "Hola"}],
        edit_count=1,
    )
    prior = _current_umg_report(job)
    prior_issue = prior["issues"][0]

    job.umg_spec = {"frame_size": "UHD-4K"}
    same_visual_issue = {"issue_id": prior_issue["issue_id"], "manual_verification_required": True}
    _merge_prior_decisions([same_visual_issue], prior, same_visual=True)
    assert same_visual_issue.get("status") == "RESOLVED_MANUAL"

    job.edit_count += 1
    next_cut_issue = {"issue_id": prior_issue["issue_id"], "manual_verification_required": True}
    _merge_prior_decisions([next_cut_issue], prior, same_visual=False)
    assert next_cut_issue.get("status") is None


def test_nonmanual_resolution_does_not_carry_to_a_different_source_snapshot():
    from delivery_qc_runtime import _merge_prior_decisions

    prior = {"issues": [{
        "issue_id": "ocr-title", "status": "ACKNOWLEDGED",
        "operator_decision": {"decision": "acknowledged", "user_id": 12},
    }]}
    current = [{"issue_id": "ocr-title", "status": "OPEN"}]
    _merge_prior_decisions(current, prior, same_source=False, same_visual=False)
    assert current[0]["status"] == "OPEN"


def test_recheck_does_not_scan_or_persist_if_source_changed_before_lock(monkeypatch):
    import database
    from delivery_qc_runtime import delivery_qc_source_fingerprint, run_delivery_qc_for_job

    job = SimpleNamespace(
        job_id="qc-race", status="done", delivery_profile="umg", workload_class="interactive",
        input_audio_sha256="a" * 64, input_r2_key="input/race.mp3",
        segments_revision=2, segments_json=[{"start": 0, "end": 1, "text": "Hola"}],
        edit_count=3, editing_started_at=None, artist="Artista", song_title="Tema",
        style="oscuro", render_params={}, scene_plan={}, bg_r2_key_cached=None,
        umg_spec={"frame_size": "HD"},
    )
    class Query:
        def filter(self, *_args): return self
        def with_for_update(self): return self
        def first(self): return job
    class Session:
        def query(self, *_args): return Query()
        def rollback(self): pass
        def close(self): pass
    monkeypatch.setattr(database, "SessionLocal", Session)
    monkeypatch.setattr(
        "delivery_qc_runtime.build_runtime_report",
        lambda **_kwargs: pytest.fail("must not scan a report against a changed source"),
    )

    result = run_delivery_qc_for_job(
        job.job_id, "/tmp/old-render.mp4", force=True,
        mode_override="enforce", expected_source_fingerprint="old-source",
    )
    assert result is None


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


def test_runtime_report_turns_missing_detectors_into_one_manual_review(tmp_path, monkeypatch):
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
    assert len(manual) == 1
    assert manual[0]["code"] == MANUAL_ATTESTATION_CODE
    assert manual[0]["severity"] == "WARN" and manual[0]["status"] == "OPEN"
    assert report["summary"]["fail_count"] == 0
    assert report["summary"]["warn_count"] == 1
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
    assert report["approval"]["reason"] == "manual_review_required"
    assert report["approval"]["blocked"] is True


def test_low_confidence_or_empty_ocr_never_marks_visible_text_as_pass(tmp_path, monkeypatch):
    asset = tmp_path / "video.mp4"
    asset.write_bytes(b"encoded")
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
            "observations": [
                {"kind": "title", "text": "", "confidence": .2},
                {"kind": "lyric", "text": "H0LA", "confidence": .7, "segment_index": 0},
            ],
            "issues": [], "abstentions": [],
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
    assert checks["ocr_title"]["status"] == "NOT_RUN"
    assert checks["ocr_lyrics"]["status"] == "NOT_RUN"


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


def test_runtime_report_preserves_detector_evidence_after_edit(tmp_path, monkeypatch):
    """Real preflight/OCR findings carry lists; older detectors use mappings."""
    monkeypatch.setenv("DELIVERY_QC_MODE", "observe")
    monkeypatch.setattr(
        "delivery_qc_runtime.inspect_delivery_media",
        lambda *_args, **_kwargs: {
            "probe": {"duration": 2.0, "video": {"fps": 30}, "audio_streams": 0},
            "issues": [{
                "code": "MEDIA_AUDIO_STREAM_MISSING", "severity": "FAIL",
                "evidence": {"audio_streams": 0},
            }],
            "abstentions": [],
        },
    )
    segments = [{"start": 0, "end": 3, "text": "JAMÁS"}]
    observations = [{
        "kind": "lyric", "segment_index": 0, "seconds": 1,
        "text": "JAMAS", "confidence": .99,
    }]
    monkeypatch.setattr(
        "delivery_qc_runtime.inspect_rendered_text",
        lambda *_args, **_kwargs: {
            "observations": observations,
            "issues": compare_ocr_observations(observations, metadata={}, segments=segments),
            "abstentions": [],
        },
    )
    job = SimpleNamespace(
        artist="Artista", song_title="Tema", filename="tema.wav", umg_spec=None,
        transcription_quality={}, segments_revision=3, edit_count=1,
    )
    previous = mark_delivery_qc_stale({"status": "COMPLETE", "issues": []}, revision=3, reason="edit")
    report = build_runtime_report(
        job=job, video_path=str(tmp_path / "video.mp4"), segments=segments, previous=previous,
    )
    assert report["status"] == "COMPLETE"
    assert report["segments_revision"] == 3
    assert report["render_identity"]["edit_count"] == 1
    checks = {row["check_id"]: row for row in report["checks"]}
    assert checks["media_audio"]["evidence"] == [{"audio_streams": 0}]
    assert checks["ocr_lyrics"]["evidence"] == [{"segment_index": 0}]
    assert checks["timeline"]["evidence"] == [{"segment_index": 0, "start": 0.0, "end": 3.0, "duration": 2.0}]
    assert checks["timeline"]["status"] == "FAIL"
    assert report["approval"]["blocked"] is False
    assert approval_gate(report, "enforce")["blocked"] is True
