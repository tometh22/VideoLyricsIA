"""An operator's second click must not start a second multi-GB transcode."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

import queue_jobs


def _fake_rq_job(status, started_ago=None):
    job = MagicMock()
    job.id = "prewarm:job1:umg_master"
    job.get_status.return_value = status
    job.started_at = (datetime.now(timezone.utc) - timedelta(seconds=started_ago)) if started_ago is not None else None
    return job


@pytest.fixture
def queue(monkeypatch):
    q = MagicMock()
    q.count = 0
    q.enqueue.return_value = MagicMock(id="prewarm:job1:umg_master")
    monkeypatch.setattr(queue_jobs, "_init_redis", lambda: (object(), object(), q))
    return q


def _existing(monkeypatch, job):
    import rq.job

    def fetch(rq_id, connection=None):
        if job is None:
            raise rq.exceptions.NoSuchJobError(rq_id)
        return job

    monkeypatch.setattr(rq.job.Job, "fetch", staticmethod(fetch))


@pytest.mark.parametrize("status", ["queued", "started", "scheduled", "deferred"])
def test_a_click_while_the_same_transcode_is_live_does_not_enqueue_another(queue, monkeypatch, status):
    _existing(monkeypatch, _fake_rq_job(status, started_ago=30 if status == "started" else None))
    assert queue_jobs.enqueue_prores_prewarm("job1", "umg_master", force=True, dedupe_live=True) == "prewarm:job1:umg_master"
    queue.enqueue.assert_not_called()


@pytest.mark.parametrize("status", ["finished", "failed", "canceled", "stopped"])
def test_a_finished_failed_or_cancelled_transcode_can_be_asked_again(queue, monkeypatch, status):
    _existing(monkeypatch, _fake_rq_job(status))
    queue_jobs.enqueue_prores_prewarm("job1", "umg_master", force=True, dedupe_live=True)
    queue.enqueue.assert_called_once()


def test_no_previous_job_enqueues_normally(queue, monkeypatch):
    _existing(monkeypatch, None)
    queue_jobs.enqueue_prores_prewarm("job1", "umg_master", force=True, dedupe_live=True)
    queue.enqueue.assert_called_once()


def test_a_dead_workers_started_entry_never_blocks_a_retry(queue, monkeypatch):
    _existing(monkeypatch, _fake_rq_job("started", started_ago=queue_jobs.PRORES_PREWARM_TIMEOUT + 600))
    queue_jobs.enqueue_prores_prewarm("job1", "umg_master", force=True, dedupe_live=True)
    queue.enqueue.assert_called_once()


def test_the_edit_pipeline_still_re_runs_an_existing_id(queue, monkeypatch):
    """Without dedupe_live the old behaviour holds: the post-edit re-warm relies on it."""
    _existing(monkeypatch, _fake_rq_job("started", started_ago=5))
    queue_jobs.enqueue_prores_prewarm("job1", "umg_master", force=True)
    queue.enqueue.assert_called_once()


def test_an_unreadable_redis_state_falls_back_to_enqueueing(queue, monkeypatch):
    import rq.job

    def boom(rq_id, connection=None):
        raise RuntimeError("redis hiccup")

    monkeypatch.setattr(rq.job.Job, "fetch", staticmethod(boom))
    queue_jobs.enqueue_prores_prewarm("job1", "umg_master", force=True, dedupe_live=True)
    queue.enqueue.assert_called_once()


def test_the_click_driven_endpoints_ask_for_the_guard():
    import re
    from pathlib import Path
    source = (Path(__file__).parents[1] / "main.py").read_text()
    calls = re.findall(r"enqueue_prores_prewarm\((.*?)\)", source, re.S)
    clicked = [c for c in calls if "force=True" in c]
    assert clicked and all("dedupe_live=True" in c for c in clicked)
