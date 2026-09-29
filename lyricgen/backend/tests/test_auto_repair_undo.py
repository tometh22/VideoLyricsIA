"""Automatic repair undo must be bound to immutable audio and editor state."""

from copy import deepcopy
import uuid

import pytest

from auth import create_user, start_login_session
from database import AuditLog, EditorDocument, EditorVersion, Job, JobOutboxEvent, SessionLocal
from editor import attach_machine_evidence, ensure_document
from machine_evidence import build_machine_evidence, finalize_machine_evidence, snapshot_hash
from tests.conftest import auth
from transcription_quality import segments_hash


SOURCE = [{"start": 0.1, "end": 1.1, "text": "versión anterior"}]
REPAIRED = [{"start": 0.2, "end": 1.2, "text": "versión automática"}]
AUDIO_SHA = "a" * 64


def _prepared_job():
    db = SessionLocal()
    try:
        tenant = f"undo_{uuid.uuid4().hex[:8]}"
        user = create_user(
            db, f"undo_{uuid.uuid4().hex[:8]}", "testpass12345", None,
            tenant_id=tenant,
        )
        token = start_login_session(db, user)
        job_id = uuid.uuid4().hex[:12]
        trace = {
            "schema": "lyrics-auto-repair-trace-v1",
            "status": "applied",
            "applied_count": 1,
            "source_segments_revision": 0,
            "source_segments_hash": segments_hash(SOURCE),
            "source_snapshot_sha256": snapshot_hash(SOURCE),
            "result_segments_hash": segments_hash(REPAIRED),
        }
        quality = {
            "decision": "review_required", "policy_version": "lyrics-quality-v6",
            "auto_repair": trace,
        }
        job = Job(
            job_id=job_id, user_id=user.id, tenant_id=tenant,
            artist="Artist", song_title="Song", filename="song.wav",
            style="oscuro", status="transcribed_pending", current_step="editing",
            delivery_profile="youtube", segments_json=deepcopy(REPAIRED),
            segments_revision=0, input_audio_sha256=AUDIO_SHA, audio_revision=1,
            transcription_quality=quality, machine_snapshot_required=True,
        )
        db.add(job)
        db.flush()
        document = ensure_document(
            db, job_id, tenant, deepcopy(REPAIRED), initial_reason="transcription",
        )
        captured = build_machine_evidence({
            "segments": deepcopy(REPAIRED),
            "_pre_auto_repair_segments": deepcopy(SOURCE),
        })
        evidence = finalize_machine_evidence(
            captured, original_segments=document.original_segments,
            quality=quality, audio_sha256=AUDIO_SHA, audio_revision=1,
        )
        attach_machine_evidence(db, document, evidence)
        db.commit()
        return job_id, token, tenant
    finally:
        db.close()


def _undo(client, job_id, token, revision=0):
    return client.post(
        f"/editor/{job_id}/auto-repair/undo",
        headers=auth(token), json={"base_revision": revision},
    )


def test_undo_survives_quality_replay_and_preserves_training_provenance(
    client, monkeypatch,
):
    import main

    monkeypatch.setattr(main, "_dispatch_editor_quality_outbox", lambda _id: None)
    job_id, token, _tenant = _prepared_job()
    with SessionLocal() as db:
        job = db.query(Job).filter_by(job_id=job_id).one()
        # Async quality analysis replaces the mutable summary after capture.
        job.transcription_quality = {"decision": "review_required"}
        db.commit()

    loaded = client.get(f"/editor/{job_id}", headers=auth(token))
    assert loaded.status_code == 200
    assert loaded.json()["auto_repair_undo_available"] is True
    assert loaded.json()["segments"] == REPAIRED
    assert "machine_evidence" not in loaded.json()

    undone = _undo(client, job_id, token)
    assert undone.status_code == 200, undone.text
    assert undone.json()["revision"] == 1
    assert undone.json()["segments"] == SOURCE
    assert undone.json()["auto_repair_undo_available"] is False
    assert client.get(f"/editor/{job_id}", headers=auth(token)).json()[
        "auto_repair_undo_available"
    ] is False
    assert _undo(client, job_id, token).status_code == 409

    with SessionLocal() as db:
        job = db.query(Job).filter_by(job_id=job_id).one()
        document = db.query(EditorDocument).filter_by(job_id=job_id).one()
        version = db.query(EditorVersion).filter_by(job_id=job_id, revision=1).one()
        assert document.current_segments == job.segments_json == SOURCE
        assert version.reason == "auto_repair_undo"
        assert job.transcription_quality["analysis_status"] == "superseded_by_edit"
        assert job.transcription_quality["render_blocked"] is True
        outbox = db.query(JobOutboxEvent).filter_by(
            job_id=job_id, event_type="quality.enqueue",
        ).one()
        assert outbox.payload["expected_revision"] == 1
        assert outbox.payload["reason"] == "auto_repair_undone"
        assert "segments" not in outbox.payload
        events = db.query(AuditLog).all()
        assert any(
            event.action == "editor.auto_repair_undone"
            and event.detail.get("job_id") == job_id
            for event in events
        )
        assert not any(
            event.action in {"lyrics.segments_diff", "lyrics.prospective_timing"}
            and event.detail.get("job_id") == job_id
            for event in events
        )


@pytest.mark.parametrize("corruption", ["missing", "changed_events", "changed_hash"])
def test_undo_rejects_missing_or_tampered_private_snapshot(client, corruption):
    job_id, token, _tenant = _prepared_job()
    with SessionLocal() as db:
        document = db.query(EditorDocument).filter_by(job_id=job_id).one()
        evidence = deepcopy(document.machine_evidence)
        source = next(
            item for item in evidence["hypotheses_by_family"]
            if item["role"] == "pre_auto_repair"
        )
        if corruption == "missing":
            evidence["hypotheses_by_family"].remove(source)
        elif corruption == "changed_events":
            source["events"][0]["text"] = "texto alterado"
        else:
            evidence["decisions"]["quality"]["auto_repair"][
                "source_snapshot_sha256"
            ] = "0" * 64
        document.machine_evidence = evidence
        db.commit()

    assert client.get(f"/editor/{job_id}", headers=auth(token)).json()[
        "auto_repair_undo_available"
    ] is False
    assert _undo(client, job_id, token).status_code == 409
    with SessionLocal() as db:
        document = db.query(EditorDocument).filter_by(job_id=job_id).one()
        assert document.revision == 0
        assert document.current_segments == REPAIRED


@pytest.mark.parametrize("change", ["audio_sha", "audio_revision", "status", "approved"])
def test_undo_rejects_changed_audio_or_finalized_job(client, change):
    job_id, token, _tenant = _prepared_job()
    with SessionLocal() as db:
        job = db.query(Job).filter_by(job_id=job_id).one()
        if change == "audio_sha":
            job.input_audio_sha256 = "b" * 64
        elif change == "audio_revision":
            job.audio_revision = 2
        elif change == "status":
            job.status = "done"
        else:
            from datetime import datetime, timezone
            job.approved_at = datetime.now(timezone.utc)
        db.commit()
    assert _undo(client, job_id, token).status_code == 409


def test_undo_rejects_stale_revision_after_human_autosave(client, monkeypatch):
    import main

    monkeypatch.setattr(main, "_dispatch_editor_quality_outbox", lambda _id: None)
    job_id, token, _tenant = _prepared_job()
    human = [{"start": 0.2, "end": 1.2, "text": "corrección humana"}]
    saved = client.patch(
        f"/editor/{job_id}", headers=auth(token),
        json={"base_revision": 0, "segments": human, "checkpoint": "autosave"},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["revision"] == 1
    assert _undo(client, job_id, token, revision=0).status_code == 409
    with SessionLocal() as db:
        document = db.query(EditorDocument).filter_by(job_id=job_id).one()
        assert document.current_segments == human
        assert document.revision == 1


def test_undo_is_tenant_scoped(client):
    job_id, _token, _tenant = _prepared_job()
    with SessionLocal() as db:
        outsider = create_user(
            db, f"outsider_{uuid.uuid4().hex[:8]}", "testpass12345", None,
            tenant_id=f"other_{uuid.uuid4().hex[:8]}",
        )
        outsider_token = start_login_session(db, outsider)
    assert _undo(client, job_id, outsider_token).status_code == 404
    with SessionLocal() as db:
        assert db.query(EditorDocument).filter_by(job_id=job_id).one().revision == 0


def test_undo_rejects_invalid_actor_without_server_error(client, monkeypatch):
    import main

    job_id, token, _tenant = _prepared_job()
    monkeypatch.setattr(
        main, "save_document",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("invalid_actor")),
    )
    response = _undo(client, job_id, token)
    assert response.status_code == 422
    assert response.json()["detail"] == "invalid_actor"
    with SessionLocal() as db:
        assert db.query(EditorDocument).filter_by(job_id=job_id).one().revision == 0
