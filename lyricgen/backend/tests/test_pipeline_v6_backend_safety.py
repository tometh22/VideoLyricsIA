"""Backend/concurrency regression coverage for the v6 delivery contracts."""

import ast
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateColumn

import queue_jobs
import transactional_outbox
import worker
from database import (
    Job, JobOutboxEvent, QUALITY_ATTEMPT_ID_MAX_LENGTH, SessionLocal, User,
    validate_quality_attempt_id_column,
)


def _seed_outbox_event(*, status="pending"):
    from auth import create_user

    db = SessionLocal()
    try:
        job_id = uuid.uuid4().hex[:12]
        user = db.query(User).filter(User.username == "admin").first()
        if user is None:
            user = create_user(
                db, "admin", "testadmin123", None, role="admin",
                tenant_id="default", plan="unlimited", enforce_reserved=False,
            )
        db.add(Job(
            job_id=job_id, user_id=user.id, tenant_id="outbox-tests",
            artist="Test", song_title="Outbox", filename="song.wav",
        ))
        db.flush()
        event = transactional_outbox.create_outbox_event(
            db, job_id=job_id, event_type="edit.enqueue",
            dedupe_key=f"edit-test:{job_id}",
            payload={"edit_type": "metadata", "edit_params": {}},
        )
        event.status = status
        event_id, dedupe_key = event.id, event.dedupe_key
        db.commit()
        return job_id, event_id, dedupe_key
    finally:
        db.close()


def test_postgresql_attempt_id_contract_is_not_masked_by_sqlite():
    ddl = str(CreateColumn(Job.__table__.c.active_quality_attempt_id).compile(
        dialect=postgresql.dialect(),
    ))
    assert f"VARCHAR({QUALITY_ATTEMPT_ID_MAX_LENGTH})" in ddl
    validate_quality_attempt_id_column("character varying", 160)
    validate_quality_attempt_id_column("text", None)
    with pytest.raises(RuntimeError, match="too short"):
        validate_quality_attempt_id_column("character varying", 64)
    with pytest.raises(RuntimeError, match="TEXT or VARCHAR"):
        validate_quality_attempt_id_column("integer", None)


def test_v6_migration_json_is_portable_and_attempt_id_is_wide(monkeypatch):
    path = Path(__file__).parents[1] / "alembic" / "versions" / (
        "b6c7d8e9f0a1_pipeline_v6_identity_outbox_proposals.py"
    )
    spec = importlib.util.spec_from_file_location("migration_b6c", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # The runtime test image need not install Alembic; the migration's type
    # contract is independent from its operation proxy.
    monkeypatch.setitem(sys.modules, "alembic", SimpleNamespace(op=object()))
    spec.loader.exec_module(module)
    portable = module._portable_json()
    assert str(portable.compile(dialect=sqlite.dialect())).upper() == "JSON"
    assert str(portable.compile(dialect=postgresql.dialect())).upper() == "JSONB"


def test_quality_attempt_id_is_bounded_for_postgresql():
    attempt_id = queue_jobs._transcription_quality_attempt_id(
        "x" * 500, expected_revision=2**63 - 1,
        expected_segments_hash="f" * 4096,
        expected_audio_revision=2**63 - 1,
        expected_audio_sha256="a" * 64,
        runtime_token="r" * 1000, publication_id="p" * 1000,
    )
    assert len(attempt_id) <= QUALITY_ATTEMPT_ID_MAX_LENGTH


def test_operational_and_quality_reconcilers_are_queue_isolated(monkeypatch):
    seen = []

    class FakeQueue:
        def __init__(self, name, connection=None):
            self.name = name

        def enqueue_in(self, *args, **kwargs):
            seen.append((self.name, kwargs["job_id"]))
            return type("Queued", (), {"id": kwargs["job_id"]})()

    monkeypatch.setenv("TRANSCRIPTION_QUALITY_QUEUE_ENABLED", "1")
    monkeypatch.setattr(queue_jobs, "_init_redis", lambda: None)
    monkeypatch.setattr(queue_jobs, "_redis", object())
    monkeypatch.setattr(queue_jobs, "_active_rq_job", lambda *_args: None)
    monkeypatch.setattr(queue_jobs, "_evict_stale_rq_job", lambda *_args: None)
    monkeypatch.setattr("rq.Queue", FakeQueue)
    queue_jobs.ensure_job_outbox_reconciler_scheduled()
    queue_jobs.ensure_quality_pending_reconciler_scheduled()
    assert seen[0][0] == "default"
    assert seen[1][0] == "transcription_quality"


def test_worker_scheduler_failures_are_isolated_and_queue_scoped(monkeypatch):
    calls = []
    monkeypatch.setattr(
        queue_jobs, "ensure_daily_quality_learning_scheduled",
        lambda: (_ for _ in ()).throw(RuntimeError("miner unavailable")),
    )
    monkeypatch.setattr(
        queue_jobs, "ensure_quality_pending_reconciler_scheduled",
        lambda: calls.append("quality_pending"),
    )
    monkeypatch.setattr(
        queue_jobs, "ensure_job_outbox_reconciler_scheduled",
        lambda: calls.append("job_outbox"),
    )
    result = worker._schedule_worker_maintenance(
        ["transcription_quality", "default"],
    )
    assert calls == ["quality_pending", "job_outbox"]
    assert result == {
        "quality_learning": False, "quality_pending": True, "job_outbox": True,
    }
    calls.clear()
    worker._schedule_worker_maintenance(["transcription_quality"])
    assert calls == ["quality_pending"]
    calls.clear()
    worker._schedule_worker_maintenance(["default"])
    assert calls == ["job_outbox"]


def test_edit_publication_recovers_ambiguous_timeout_without_duplicate(monkeypatch):
    registry = {}
    enqueue_calls = []

    class FakeQueue:
        connection = object()

        def enqueue(self, _task, *args, **kwargs):
            enqueue_calls.append(kwargs["job_id"])
            queued = type("Queued", (), {"id": kwargs["job_id"]})()
            registry[kwargs["job_id"]] = queued
            raise TimeoutError("reply lost after Redis accepted the job")

    queue = FakeQueue()
    monkeypatch.setattr(queue_jobs, "_require_submissions_open", lambda: None)
    monkeypatch.setattr(queue_jobs, "_pick_queue", lambda *_args, **_kwargs: queue)
    monkeypatch.setattr(
        queue_jobs, "_active_rq_job",
        lambda _connection, rq_id: registry.get(rq_id),
    )
    monkeypatch.setattr(queue_jobs, "_evict_stale_rq_job", lambda *_args: None)
    publication_id = str(uuid.uuid4())
    kwargs = dict(
        job_id="ambiguous", edit_type="metadata", edit_params={},
        publication_id=publication_id, publication_dedupe_key="dedupe:ambiguous",
    )
    with pytest.raises(TimeoutError):
        queue_jobs.enqueue_edit(**kwargs)
    recovered = queue_jobs.enqueue_edit(**kwargs)
    assert recovered == f"edit-outbox:{publication_id}"
    assert enqueue_calls == [f"edit-outbox:{publication_id}"]


def test_edit_publication_replaces_terminal_rq_job_during_stale_recovery(
        monkeypatch):
    enqueue_calls = []
    evictions = []

    class FakeQueue:
        connection = object()

        def enqueue(self, _task, *args, **kwargs):
            enqueue_calls.append(kwargs["job_id"])
            return SimpleNamespace(id=kwargs["job_id"])

    queue = FakeQueue()
    monkeypatch.setattr(queue_jobs, "_require_submissions_open", lambda: None)
    monkeypatch.setattr(queue_jobs, "_pick_queue", lambda *_args, **_kwargs: queue)
    # A failed/completed RQ row is deliberately not an active delivery.
    monkeypatch.setattr(queue_jobs, "_active_rq_job", lambda *_args: None)
    monkeypatch.setattr(
        queue_jobs, "_evict_stale_rq_job",
        lambda _connection, rq_id: evictions.append(rq_id),
    )
    publication_id = str(uuid.uuid4())

    result = queue_jobs.enqueue_edit(
        job_id="recovered", edit_type="metadata", edit_params={},
        publication_id=publication_id,
        publication_dedupe_key="dedupe:terminal-recovery",
    )

    expected = f"edit-outbox:{publication_id}"
    assert result == expected
    assert evictions == [expected]
    assert enqueue_calls == [expected]


def test_outbox_consumer_uses_event_identity_under_concurrency(monkeypatch):
    _job_id, event_id, dedupe_key = _seed_outbox_event(status="dispatched")
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def fake_pipeline(*_args, **_kwargs):
        calls.append("run")
        entered.set()
        assert release.wait(5)
        return "ok"

    monkeypatch.setattr("pipeline.run_edit_pipeline", fake_pipeline)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            transactional_outbox.run_outbox_edit_pipeline,
            _job_id, event_id, dedupe_key, "metadata", {}, "policy",
        )
        assert entered.wait(5)
        second = pool.submit(
            transactional_outbox.run_outbox_edit_pipeline,
            _job_id, event_id, dedupe_key, "metadata", {}, "policy",
        )
        with pytest.raises(RuntimeError, match="outbox_consumer_busy"):
            second.result(timeout=5)
        release.set()
        assert first.result(timeout=5) == "ok"
    db = SessionLocal()
    try:
        event = db.query(JobOutboxEvent).filter(JobOutboxEvent.id == event_id).one()
        assert event.status == "consumed"
        assert event.consumed_at is not None
        assert calls == ["run"]
    finally:
        db.close()


def test_outbox_consumer_lease_blocks_duplicates_through_render_timeout(
    monkeypatch,
):
    _job_id, event_id, dedupe_key = _seed_outbox_event(status="dispatched")
    started_at = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
    clock = [started_at]
    monkeypatch.setattr(transactional_outbox, "_now", lambda: clock[0])
    monkeypatch.setenv("JOB_OUTBOX_CONSUMER_LEASE_SECONDS", "45")

    assert transactional_outbox._outbox_consumer_lease_seconds() == 3900
    assert transactional_outbox._claim_outbox_consumer(
        event_id, dedupe_key, "first", lease_seconds=45,
    ) == "claimed"

    clock[0] = started_at + timedelta(seconds=46)
    assert transactional_outbox._claim_outbox_consumer(
        event_id, dedupe_key, "second", lease_seconds=45,
    ) == "busy"

    clock[0] = started_at + timedelta(seconds=3900)
    assert transactional_outbox._claim_outbox_consumer(
        event_id, dedupe_key, "third", lease_seconds=45,
    ) == "busy"

    clock[0] = started_at + timedelta(seconds=3900, microseconds=1)
    assert transactional_outbox._claim_outbox_consumer(
        event_id, dedupe_key, "fourth", lease_seconds=45,
    ) == "claimed"


def test_worker_schema_gate_requires_outbox_consumer_columns():
    path = Path(__file__).parents[1] / "scripts" / "require_worker_schema.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    required_columns = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "REQUIRED_COLUMNS"
            for target in node.targets
        ):
            required_columns = ast.literal_eval(node.value)
            break
    assert required_columns is not None
    assert {
        ("job_outbox_events", "processing_at"),
        ("job_outbox_events", "processing_token"),
        ("job_outbox_events", "consumed_at"),
    } <= required_columns


def test_reconciler_reschedules_even_when_dispatch_fails(monkeypatch):
    scheduled = []
    monkeypatch.setattr(
        transactional_outbox, "dispatch_pending_outbox_events",
        lambda: (_ for _ in ()).throw(RuntimeError("database timeout")),
    )
    monkeypatch.setattr(
        queue_jobs, "ensure_job_outbox_reconciler_scheduled",
        lambda: scheduled.append("scheduled"),
    )
    with pytest.raises(RuntimeError, match="database timeout"):
        transactional_outbox.reconcile_job_outbox()
    assert scheduled == ["scheduled"]


def test_stale_outbox_consumer_is_recovered_without_touching_quality_queue(monkeypatch):
    _job_id, event_id, _dedupe = _seed_outbox_event(status="processing")
    db = SessionLocal()
    try:
        event = db.query(JobOutboxEvent).filter(JobOutboxEvent.id == event_id).one()
        event.processing_at = datetime.now(timezone.utc) - timedelta(hours=2)
        event.processing_token = str(uuid.uuid4())
        db.commit()
    finally:
        db.close()
    monkeypatch.setenv("JOB_OUTBOX_STALE_PROCESSING_SECONDS", "300")
    monkeypatch.setattr(
        transactional_outbox, "_publish",
        lambda _event, **_kwargs: "rq:recovered",
    )
    result = transactional_outbox.dispatch_pending_outbox_events()
    assert result["dispatched"] >= 1
    verify = SessionLocal()
    try:
        event = verify.query(JobOutboxEvent).filter(JobOutboxEvent.id == event_id).one()
        assert event.status == "dispatched"
        assert event.last_error is None
    finally:
        verify.close()


def test_outbox_transient_failures_become_terminal_and_keep_error_code(monkeypatch):
    _job_id, event_id, _dedupe = _seed_outbox_event()
    monkeypatch.setenv("JOB_OUTBOX_MAX_ATTEMPTS", "2")
    monkeypatch.setattr(
        transactional_outbox, "_publish",
        lambda _event, **_kwargs: (_ for _ in ()).throw(
            ConnectionError("redis details must not be persisted")
        ),
    )

    first = transactional_outbox.dispatch_outbox_event(event_id)
    assert first["status"] == "pending"
    db = SessionLocal()
    try:
        event = db.query(JobOutboxEvent).filter(JobOutboxEvent.id == event_id).one()
        event.available_at = datetime.now(timezone.utc)
        db.commit()
    finally:
        db.close()
    second = transactional_outbox.dispatch_outbox_event(event_id)
    assert second["status"] == "failed"

    db = SessionLocal()
    try:
        event = db.query(JobOutboxEvent).filter(JobOutboxEvent.id == event_id).one()
        assert event.status == "failed"
        assert event.last_error == "ConnectionError:attempts=2"
    finally:
        db.close()


def test_quality_rollout_skip_is_terminal(monkeypatch):
    _job_id, event_id, _dedupe = _seed_outbox_event()
    monkeypatch.setattr(
        transactional_outbox, "_publish",
        lambda _event, **_kwargs: (_ for _ in ()).throw(
            transactional_outbox.OutboxDeliveryError(
                "disabled_quality_delivery", retryable=False,
            )
        ),
    )
    result = transactional_outbox.dispatch_outbox_event(event_id)
    assert result["status"] == "skipped"
    db = SessionLocal()
    try:
        event = db.query(JobOutboxEvent).filter(JobOutboxEvent.id == event_id).one()
        assert event.status == "skipped"
        assert event.last_error == "disabled_quality_delivery:attempts=1"
    finally:
        db.close()


def test_legacy_audio_identity_backfill_is_content_addressed_and_cas_safe(monkeypatch):
    import storage

    job_id, _event_id, _dedupe = _seed_outbox_event()
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.job_id == job_id).one()
        job.input_r2_key = f"legacy/{job_id}/song.wav"
        db.commit()
    finally:
        db.close()
    audio = b"immutable legacy audio"
    digest = hashlib.sha256(audio).hexdigest()
    uploads = []

    def download(_key, path):
        Path(path).write_bytes(audio)
        return True

    monkeypatch.setattr(storage, "is_enabled", lambda: True)
    monkeypatch.setattr(storage, "download_object", download)
    monkeypatch.setattr(
        storage, "content_addressed_input_key",
        lambda tenant, job, sha, filename: f"inputs/{tenant}/{job}/{sha}/{filename}",
    )
    monkeypatch.setattr(
        storage, "upload_file", lambda path, key: uploads.append(key) or key,
    )
    monkeypatch.setattr(storage, "object_etag", lambda _key: "etag-v1")
    identity = queue_jobs.ensure_legacy_audio_identity(job_id)
    assert identity == {"audio_revision": 1, "audio_sha256": digest}
    verify = SessionLocal()
    try:
        row = verify.query(Job).filter(Job.job_id == job_id).one()
        assert row.input_r2_key == uploads[0]
        assert row.input_audio_sha256 == digest
        assert row.input_audio_etag == "etag-v1"
        assert row.audio_revision == 1
    finally:
        verify.close()


def test_legacy_audio_backfill_does_not_overwrite_concurrent_restore(monkeypatch):
    import storage

    job_id, _event_id, _dedupe = _seed_outbox_event()
    legacy_key = f"legacy/{job_id}/song.wav"
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.job_id == job_id).one()
        job.input_r2_key = legacy_key
        db.commit()
    finally:
        db.close()

    def download(_key, path):
        Path(path).write_bytes(b"old audio")
        return True

    def upload(_path, key):
        concurrent = SessionLocal()
        try:
            row = concurrent.query(Job).filter(Job.job_id == job_id).one()
            row.input_r2_key = f"restored/{job_id}/new.wav"
            row.input_audio_sha256 = "9" * 64
            row.audio_revision = 7
            concurrent.commit()
        finally:
            concurrent.close()
        return key

    monkeypatch.setattr(storage, "is_enabled", lambda: True)
    monkeypatch.setattr(storage, "download_object", download)
    monkeypatch.setattr(
        storage, "content_addressed_input_key",
        lambda tenant, job, sha, filename: f"inputs/{tenant}/{job}/{sha}/{filename}",
    )
    monkeypatch.setattr(storage, "upload_file", upload)
    monkeypatch.setattr(storage, "object_etag", lambda _key: "old-etag")
    # The newer authoritative identity wins; backfill returns it and never
    # points the row back at the old mutable object.
    assert queue_jobs.ensure_legacy_audio_identity(job_id) == {
        "audio_revision": 7, "audio_sha256": "9" * 64,
    }
    verify = SessionLocal()
    try:
        row = verify.query(Job).filter(Job.job_id == job_id).one()
        assert row.input_r2_key == f"restored/{job_id}/new.wav"
        assert row.input_audio_sha256 == "9" * 64
        assert row.audio_revision == 7
    finally:
        verify.close()
