"""Regression coverage for durable render/transcription publication fences."""

import uuid

from auth import create_user
from database import Job, JobOutboxEvent, SessionLocal, User
from jobs import bind_job_attempt, update_job
from transactional_outbox import (
    create_pipeline_outbox_event,
    create_transcription_outbox_event,
    dispatch_outbox_event,
)


def _job():
    db = SessionLocal()
    user = db.query(User).filter(User.username == "outbox-fence-admin").first()
    if user is None:
        user = create_user(
            db, "outbox-fence-admin", "testadmin123", None, role="admin",
            tenant_id="outbox-fence", plan="unlimited", enforce_reserved=False,
        )
    job = Job(
        job_id=uuid.uuid4().hex[:12], user_id=user.id,
        tenant_id="outbox-fence", artist="Test", filename="song.wav",
        status="queued", current_step="queued", progress=1,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return db, job


def test_new_pipeline_attempt_fences_late_worker_updates():
    db, job = _job()
    try:
        first = create_pipeline_outbox_event(
            db, job=job, purpose="generate", mp3_path=None, artist="Test",
            style="oscuro", plan="100", tenant_id="outbox-fence",
            pipeline_kwargs={},
        )
        second = create_pipeline_outbox_event(
            db, job=job, purpose="retry", mp3_path=None, artist="Test",
            style="oscuro", plan="100", tenant_id="outbox-fence",
            pipeline_kwargs={},
        )
        db.commit()

        with bind_job_attempt("pipeline", first.id):
            update_job(job.job_id, status="done", progress=100)
        db.refresh(job)
        assert job.status == "queued"

        with bind_job_attempt("pipeline", second.id):
            update_job(job.job_id, status="done", progress=100)
        db.refresh(job)
        assert job.status == "done"
    finally:
        db.query(JobOutboxEvent).filter(JobOutboxEvent.job_id == job.job_id).delete()
        db.delete(job)
        db.commit()
        db.close()


def test_dispatch_passes_transcription_publication_identity():
    db, job = _job()
    try:
        event = create_transcription_outbox_event(
            db, job=job, audio_path="/tmp/audio.wav",
            transcription_kwargs={"language": "es"},
        )
        event_id = event.id
        db.commit()
        captured = {}

        def publisher(job_id, audio_path, **kwargs):
            captured.update(job_id=job_id, audio_path=audio_path, **kwargs)
            return "transcribe:attempt"

        result = dispatch_outbox_event(
            event_id, transcription_publisher=publisher,
        )
        assert result["status"] == "dispatched"
        assert captured["publication_id"] == event_id
        assert captured["job_id"] == job.job_id
        assert captured["audio_path"] == "/tmp/audio.wav"
    finally:
        db.query(JobOutboxEvent).filter(JobOutboxEvent.job_id == job.job_id).delete()
        db.delete(job)
        db.commit()
        db.close()
