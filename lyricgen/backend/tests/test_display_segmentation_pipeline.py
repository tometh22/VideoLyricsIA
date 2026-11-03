"""Finalizer integration: remeasurement, evidence, stale jobs and rollback."""
import asyncio
from copy import deepcopy
import uuid

import pytest

import display_segmentation_policy
import transcription_quality
import transcription_worker as worker
from database import Job
from editor import auto_repair_undo_segments, ensure_document, save_document
from machine_evidence import finalize_machine_evidence, validate_machine_evidence, MachineSnapshotMissing
from tests.test_display_segmentation import line, SOURCE


@pytest.fixture
def finalizer(monkeypatch):
    monkeypatch.setenv("DISPLAY_SEGMENTATION_MODE", "enforce")
    monkeypatch.setenv("LYRIC_AUTO_REPAIR_ENABLED", "0")
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_INLINE_RETRY", "0")
    monkeypatch.setenv("LINE_SIGNALS_V2_ENABLED", "0")
    monkeypatch.setattr(display_segmentation_policy, "runtime_authorization", lambda: {"authorized": True, "status": "authorized", "sha256": "f" * 64})
    monkeypatch.setattr(transcription_quality, "calibration_identity", lambda: {"calibrated": False})
    measured = []

    def measure(result, *_args, **kwargs):
        measured.append((len(result["segments"]), kwargs.get("allow_timing_mutation", True)))
        result = deepcopy(result)
        result["postpass_stats"] = {"coverage_final": {"audio_coverage": 1}, "quality_windows": []}
        return result
    monkeypatch.setattr(worker, "_medir_cobertura_final", measure)
    return measured


def finish(rows=None, **kwargs):
    return asyncio.run(worker._quality_gate_and_retry(
        {"segments": rows or [line()]}, "/tmp/not-read.wav", SOURCE["job_id"], "es", None,
        lambda r, _job: r, source_audio_sha256=SOURCE["audio_sha256"],
        source_audio_revision=1, fresh_transcription=True, **kwargs,
    ))


def test_shared_finalizer_freezes_exact_pre_and_post_and_remeasures_without_retiming(finalizer):
    result = finish()
    assert len(result["segments"]) > 1
    assert finalizer[0] == (1, True)
    assert finalizer[-1][0] > 1 and finalizer[-1][1] is False
    quality = result["transcription_quality"]
    report = quality["display_segmentation"]
    assert report["applied_count"] == 1
    assert report["source"]["audio_revision"] == 1
    assert "_pre_display_segmentation_segments" not in result
    evidence = finalize_machine_evidence(
        result["_machine_evidence"], original_segments=result["segments"], quality=quality,
        audio_sha256=SOURCE["audio_sha256"], audio_revision=1,
    )
    validate_machine_evidence(evidence, result["segments"])
    for field in ("display_segmentation_proposal", "hypotheses_by_family"):
        assert field in evidence
    broken = deepcopy(evidence)
    broken["display_segmentation_proposal"]["changes"][0]["ranges"] = [[0, 1]]
    with pytest.raises(MachineSnapshotMissing):
        validate_machine_evidence(broken, result["segments"])


def test_remeasurement_cannot_silently_move_final_boundaries(finalizer, monkeypatch):
    measure = worker._medir_cobertura_final
    def changing(result, *args, **kwargs):
        if kwargs.get("allow_timing_mutation") is False:
            result["segments"][0]["end"] += 0.1
        return measure(result, *args, **kwargs)
    monkeypatch.setattr(worker, "_medir_cobertura_final", changing)
    result = finish()
    assert len(result["segments"]) == 1
    assert result["transcription_quality"]["display_segmentation"]["status"] == "unavailable"


def test_protected_line_is_reviewed_not_split_even_alongside_valid_candidates(finalizer):
    rows = [line(), line(start=12, identity="protected")]
    rows[1]["locked"] = True
    result = finish(rows)
    assert result["segments"][-1]["locked"] is True
    report = result["transcription_quality"]["display_segmentation"]
    assert report["applied_count"] == 1
    assert report["review"][0]["reason"] == "protected_line"


def test_fresh_job_rechecks_open_document_and_revision_zero(db):
    row = Job(job_id=uuid.uuid4().hex[:12], user_id=1, tenant_id="display-tests", artist="Test", song_title="Test", filename="song.wav",
              style="oscuro", status="transcribing", segments_revision=0, segments_json=None)
    db.add(row)
    db.flush()
    assert display_segmentation_policy.fresh_job(db, row)
    ensure_document(db, row.job_id, row.tenant_id, [], initial_reason="transcription")
    assert not display_segmentation_policy.fresh_job(db, row)


def test_generic_undo_restores_pre_division_in_new_revision(finalizer, db):
    result = finish()
    job = Job(job_id=uuid.uuid4().hex[:12], user_id=1, tenant_id="display-tests", artist="Test", song_title="Test", filename="song.wav",
              style="oscuro", status="transcribed_pending", segments_revision=0,
              segments_json=result["segments"], input_audio_sha256=SOURCE["audio_sha256"], audio_revision=1)
    db.add(job)
    db.flush()
    document = ensure_document(db, job.job_id, job.tenant_id, result["segments"], initial_reason="transcription")
    evidence = finalize_machine_evidence(result["_machine_evidence"], original_segments=document.original_segments,
                                         quality=result["transcription_quality"], audio_sha256=job.input_audio_sha256, audio_revision=1)
    document.machine_evidence = evidence
    db.flush()
    restored = auto_repair_undo_segments(job, document)
    assert restored is not None and len(restored) == 1
    document, version, applied = save_document(db, job, document, 1, 0, restored, "auto_repair_undo")
    assert applied and document.revision == 1
    assert version.reason == "auto_repair_undo"
    assert document.original_segments == result["segments"]
    assert auto_repair_undo_segments(job, document) is None


def test_word_precision_survives_editor_normalization_and_evidence_binding(finalizer, db):
    result = finish([line(start=2.123456)])
    job = Job(job_id=uuid.uuid4().hex[:12], user_id=1, tenant_id="display-tests", artist="Test", song_title="Test",
              filename="song.wav", style="oscuro", status="transcribed_pending", segments_revision=0,
              segments_json=result["segments"], input_audio_sha256=SOURCE["audio_sha256"], audio_revision=1)
    db.add(job)
    db.flush()
    document = ensure_document(db, job.job_id, job.tenant_id, result["segments"], initial_reason="transcription")
    evidence = finalize_machine_evidence(result["_machine_evidence"], original_segments=document.original_segments,
                                         quality=result["transcription_quality"], audio_sha256=job.input_audio_sha256, audio_revision=1)
    validate_machine_evidence(evidence, document.original_segments)
    document.machine_evidence = evidence
    db.flush()
    assert auto_repair_undo_segments(job, document) is not None


def test_ass_preserves_consecutive_cards_and_approved_render_never_rejoins():
    from display_segmentation import apply, plan
    from ass_render import segments_to_lines
    from pipeline import _effective_render_segments
    original = [line()]
    cards = apply(original, plan(original, source=SOURCE), source=SOURCE)
    rendered = _effective_render_segments(cards, duration=15, preserve_approved_timing=True)
    lines = segments_to_lines(rendered, text_scale=1, lyric_transition="cut")
    assert [entry.text for entry in lines] == [card["text"] for card in cards]
    assert [entry.start_s for entry in lines] == [card["start"] for card in cards]
    assert [entry.end_s for entry in lines] == [card["end"] for card in cards]


@pytest.mark.parametrize("race", ["editor_opened", "audio_changed"])
def test_inline_persistence_rejects_concurrent_changes_without_rewriting_job(finalizer, monkeypatch, tmp_path, race):
    import main
    from database import SessionLocal
    from quality_cache import sha256_file
    from fastapi import HTTPException
    path = tmp_path / "source.wav"
    path.write_bytes(b"identity-only-test-no-decoding")
    job_id = uuid.uuid4().hex[:12]
    sha = sha256_file(str(path))
    with SessionLocal() as db:
        row = Job(job_id=job_id, user_id=1, tenant_id="display-inline-tests", artist="Test", song_title="Test",
                  filename="song.wav", style="oscuro", status="transcribing", segments_revision=0,
                  segments_json=None, input_audio_sha256=sha, audio_revision=1)
        db.add(row)
        db.commit()
    real_finalize = worker._quality_gate_and_retry

    async def finish_and_race(*args, **kwargs):
        result = await real_finalize(*args, **kwargs)
        assert result["transcription_quality"]["display_segmentation"]["applied_count"]
        with SessionLocal() as db:
            row = db.query(Job).filter_by(job_id=job_id).one()
            if race == "editor_opened":
                ensure_document(db, row.job_id, row.tenant_id, [], initial_reason="transcription")
            else:
                row.input_audio_sha256 = "b" * 64
                row.audio_revision = 2
            db.commit()
        return result
    monkeypatch.setattr(worker, "_quality_gate_and_retry", finish_and_race)
    source = {"job_id": job_id, "audio_sha256": sha, "audio_revision": 1,
              "segments_revision": 0, "fresh": True}
    with pytest.raises(HTTPException) as error:
        asyncio.run(main._finalize_inline_transcription_quality(
            {"segments": [line()], "_transcription_source": source}, str(path), job_id, "es"))
    assert error.value.status_code == 409
    with SessionLocal() as db:
        row = db.query(Job).filter_by(job_id=job_id).one()
        assert not row.segments_json and row.status == "transcribing"
        assert row.input_audio_sha256 == ("b" * 64 if race == "audio_changed" else sha)


def test_frozen_display_guidance_survives_quality_replay(finalizer, db):
    from editor import serialize_document
    result = finish()
    job = Job(job_id=uuid.uuid4().hex[:12], user_id=1, tenant_id="display-tests", artist="Test", song_title="Test",
              filename="song.wav", style="oscuro", status="transcribed_pending", segments_revision=0,
              segments_json=result["segments"], input_audio_sha256=SOURCE["audio_sha256"], audio_revision=1)
    db.add(job)
    db.flush()
    document = ensure_document(db, job.job_id, job.tenant_id, result["segments"], initial_reason="transcription")
    document.machine_evidence = finalize_machine_evidence(result["_machine_evidence"], original_segments=document.original_segments,
        quality=result["transcription_quality"], audio_sha256=job.input_audio_sha256, audio_revision=1)
    job.transcription_quality = {"decision": "review_required"}
    payload = serialize_document(db, document, job)
    assert payload["display_segmentation"]["applied_count"] == 1
    assert payload["display_segmentation_current"]
    document.current_segments = [{**row, "text": row["text"] + " cambiado"} for row in document.current_segments]
    assert not serialize_document(db, document, job)["display_segmentation_current"]


def test_low_parent_confidence_cannot_be_erased_by_division(finalizer):
    row = line()
    for word in row["words"]:
        word["score"] = 0.1
    row["provider_evidence"] = {"source": "asr", "text": row["text"], "start": row["start"],
        "end": row["end"], "words": row["words"], "mean_score": 0.1, "min_score": 0.1}
    result = finish([row])
    assert len(result["segments"]) == 1
    report = result["transcription_quality"]["display_segmentation"]
    assert not report["applied_count"]
    assert report["review"][0]["reason"] == "parent_quality_review_required"
