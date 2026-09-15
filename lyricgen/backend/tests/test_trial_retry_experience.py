"""Trial regression: transient render failures must not close the progress UI."""
from types import SimpleNamespace
from unittest.mock import MagicMock
import uuid

import pytest

from database import Job, SessionLocal
from job_retry import render_failure_fields
from jobs import update_job
from queue_jobs import pipeline_failure_callback, edit_failure_callback


@pytest.mark.parametrize("budget", [None, 0, -1, "1", True, MagicMock()])
def test_unknown_or_exhausted_budget_is_terminal(budget):
    assert render_failure_fields(SimpleNamespace(retries_left=budget), error="saved") == {
        "status": "error", "error": "saved",
    }


@pytest.mark.parametrize("status", ["stopped", "canceled", "finished", "failed"])
def test_closed_rq_job_does_not_advertise_retry(status):
    assert render_failure_fields(SimpleNamespace(retries_left=1, _status=status))["status"] == "error"


@pytest.fixture
def saved_job():
    jid = uuid.uuid4().hex[:12]
    with SessionLocal() as db:
        db.add(Job(job_id=jid, user_id=1, tenant_id="trial_retry_qa", status="processing", progress=22,
                   current_step="background", artist="Trial retry QA", filename="qa.mp3",
                   segments_json=[{"text": "saved correction"}], segments_revision=1))
        db.commit()
    yield jid
    with SessionLocal() as db:
        db.query(Job).filter(Job.job_id == jid).delete()
        db.commit()


def state(jid):
    with SessionLocal() as db:
        row = db.query(Job).filter(Job.job_id == jid).one()
        return {key: getattr(row, key) for key in (
            "status", "current_step", "progress", "completed_at", "error",
            "segments_json", "segments_revision",
        )}


@pytest.mark.parametrize("callback,active", [
    (pipeline_failure_callback, "processing"), (edit_failure_callback, "editing"),
])
def test_retry_then_success_preserves_work(callback, active, saved_job):
    job = SimpleNamespace(id=saved_job, retries_left=1, meta={})
    callback(job, None, RuntimeError, RuntimeError("provider interrupted"), None)
    first = state(saved_job)
    assert first["status"] == active
    assert first["current_step"] == "retrying"
    assert first["completed_at"] is None
    assert first["error"] is None
    assert first["progress"] == 22
    assert first["segments_revision"] == 1
    update_job(saved_job, status="pending_review", progress=100)
    assert state(saved_job)["status"] == "pending_review"
    assert state(saved_job)["segments_json"] == first["segments_json"]


def test_late_retry_callback_cannot_revive_terminal_error(saved_job):
    update_job(saved_job, status="error", error="final error")
    pipeline_failure_callback(SimpleNamespace(id=saved_job, retries_left=1), None,
                              RuntimeError, RuntimeError("late"), None)
    assert state(saved_job)["status"] == "error"
    assert state(saved_job)["error"] == "final error"


def test_stale_attempt_cannot_change_new_attempt(saved_job):
    update_job(saved_job, active_pipeline_attempt_id="new-attempt")
    pipeline_failure_callback(SimpleNamespace(
        id="rq-old", retries_left=1,
        meta={"db_job_id": saved_job, "outbox_event_id": "old-attempt"},
    ), None, RuntimeError, RuntimeError("late"), None)
    assert state(saved_job)["current_step"] == "background"


def _rq_render_attempt(jid, always_fail):
    """No paid provider calls: execute real RQ retry lifecycle with injected failure."""
    from rq import get_current_job
    if always_fail or get_current_job().retries_left > 0:
        raise RuntimeError("injected transient provider outage")
    update_job(jid, status="pending_review", progress=100)


@pytest.mark.parametrize("always_fail,expected", [(False, "pending_review"), (True, "error")])
def test_real_rq_retry_is_bounded_and_publishes_terminal_only_at_end(saved_job, always_fail, expected, monkeypatch):
    from fakeredis import FakeStrictRedis
    from rq import Queue, Retry, SimpleWorker
    import jobs

    writes = []
    original = jobs.update_job
    def record(jid, **fields):
        original(jid, **fields)
        writes.append(state(jid)["status"])
    monkeypatch.setattr(jobs, "update_job", record)
    connection = FakeStrictRedis()
    queue = Queue("isolated-trial-retry-test", connection=connection)
    queued = queue.enqueue(_rq_render_attempt, saved_job, always_fail,
                           job_id=saved_job, retry=Retry(max=1, interval=0),
                           on_failure=pipeline_failure_callback)
    SimpleWorker([queue], connection=connection).work(burst=True)
    queued.refresh()
    assert queued.retries_left == 0
    assert queue.count == 0
    assert writes[0] == "processing"
    assert state(saved_job)["status"] == expected
    if always_fail:
        assert writes == ["processing", "error"]
        assert state(saved_job)["completed_at"] is not None
