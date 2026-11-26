"""Host ProRes admission: portal before prewarm, one encode slot, bounded download."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import prores_capacity as cap


BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parents[1]


def _dedicated(monkeypatch, tmp_path):
    monkeypatch.setenv("PRORES_PORTAL_QUEUE", "prores_portal")
    monkeypatch.setenv("PRORES_PREWARM_QUEUE", "prores_prewarm")
    monkeypatch.setenv("PRORES_SEMAPHORE_DIR", str(tmp_path))
    monkeypatch.setenv("PRORES_SEMAPHORE_NAME", "prores")


def test_queue_names_stay_on_enterprise_when_unset(monkeypatch):
    monkeypatch.delenv("PRORES_PORTAL_QUEUE", raising=False)
    monkeypatch.delenv("PRORES_PREWARM_QUEUE", raising=False)
    monkeypatch.delenv("PRORES_MAX_CONCURRENCY", raising=False)
    assert cap.portal_queue_name() == "enterprise"
    assert cap.prewarm_queue_name() == "enterprise"
    assert cap.coordination_enabled() is False
    assert cap.dedicated_queue_names() == []
    assert cap.max_concurrency() is None


@pytest.mark.parametrize("raw", ["", "0", "-1", "nope"])
def test_concurrency_unset_or_invalid_is_unlimited(monkeypatch, raw):
    monkeypatch.setenv("PRORES_MAX_CONCURRENCY", raw)
    assert cap.max_concurrency() is None


def test_portal_is_served_before_prewarm():
    decision = cap.decide_admission(
        gate_enabled=True,
        portal_queued=2,
        prewarm_queued=9,
        host_portal_waiting=0,
        portal_head_ready=True,
        prewarm_head_ready=True,
        concurrency_limit=1,
        slots_free=1,
    )
    assert decision.action == "encode"
    assert decision.queue == "portal"
    assert decision.takes_slot is True


def test_other_deployments_portal_demand_blocks_prewarm():
    decision = cap.decide_admission(
        gate_enabled=True,
        portal_queued=0,
        prewarm_queued=4,
        host_portal_waiting=1,
        portal_head_ready=False,
        prewarm_head_ready=True,
        concurrency_limit=1,
        slots_free=1,
    )
    assert decision.action == "wait"
    assert decision.occupies_worker is False


def test_full_slot_leaves_the_job_queued():
    decision = cap.decide_admission(
        gate_enabled=True,
        portal_queued=1,
        prewarm_queued=0,
        host_portal_waiting=0,
        portal_head_ready=True,
        prewarm_head_ready=False,
        concurrency_limit=2,
        slots_free=0,
    )
    assert decision.action == "wait"
    assert decision.queue == "portal"
    assert decision.occupies_worker is False
    assert decision.takes_slot is False


def test_download_does_not_take_the_encode_slot():
    decision = cap.decide_admission(
        gate_enabled=True,
        portal_queued=1,
        prewarm_queued=3,
        host_portal_waiting=0,
        portal_head_ready=False,
        prewarm_head_ready=True,
        concurrency_limit=1,
        slots_free=1,
    )
    assert decision.action == "download"
    assert decision.queue == "portal"
    assert decision.takes_slot is False
    assert decision.occupies_worker is True


def test_gate_stays_off_for_a_worker_that_also_listens_to_default(monkeypatch):
    monkeypatch.setenv("PRORES_PORTAL_QUEUE", "prores_portal")
    monkeypatch.setenv("PRORES_PREWARM_QUEUE", "prores_prewarm")
    assert cap.gate_enabled_for(["prores_portal", "prores_prewarm"]) is True
    assert cap.gate_enabled_for(["prores_prewarm"]) is True
    assert cap.gate_enabled_for(["enterprise", "default", "canary"]) is False
    assert cap.gate_enabled_for(["prores_portal", "default"]) is False
    assert cap.strict_queue_order() == ["prores_portal", "prores_prewarm"]


def test_marker_survives_a_retryable_failure(monkeypatch, tmp_path):
    _dedicated(monkeypatch, tmp_path)
    cap.mark_portal_demand("portal-prores-abc")

    def current():
        return type("Job", (), {"id": "portal-prores-abc"})()

    monkeypatch.setattr(cap, "_current_rq_job", current)
    with pytest.raises(RuntimeError):
        with cap.release_portal_demand_on_terminal():
            raise RuntimeError("download stalled")
    assert cap.host_portal_waiting() == 1
    with pytest.raises(cap.ProResDefer):
        with cap.release_portal_demand_on_terminal():
            raise cap.ProResDefer("encode slot busy")
    assert cap.host_portal_waiting() == 1
    with cap.release_portal_demand_on_terminal():
        pass
    assert cap.host_portal_waiting() == 0


def test_input_cache_is_keyed_by_source_and_etag(monkeypatch, tmp_path):
    monkeypatch.setenv("PRORES_INPUT_CACHE", str(tmp_path))
    first = cap.cached_input_path("tenant/job/lyric_video.mp4", "etag-v1")
    second = cap.cached_input_path("tenant/job/lyric_video.mp4", "etag-v2")
    assert first != second
    os.makedirs(os.path.dirname(first))
    with open(first, "wb") as handle:
        handle.write(b"v1")
    cap.discard_cached_input("tenant/job/lyric_video.mp4", "etag-v2")
    assert os.path.exists(first)
    cap.discard_cached_input("tenant/job/lyric_video.mp4", "etag-v1")
    assert not os.path.exists(first)
    portal = (BACKEND / "portal_prores.py").read_text()
    assert "cached_input_path(source_key, etag)" in portal
    assert "discard_cached_input(source_key, etag)" in portal


def test_customer_click_promotes_a_queued_background_prewarm(monkeypatch, tmp_path):
    import queue_jobs
    from unittest.mock import Mock

    _dedicated(monkeypatch, tmp_path)
    live = type("Live", (), {})()
    live.id = "prewarm:job1:umg_master"
    live.origin = "prores_prewarm"
    live.meta = {}
    live.get_status = lambda refresh=True: "queued"
    live.save_meta = lambda: None
    portal = type("Q", (), {"name": "prores_portal", "count": 0})()
    portal.connection = object()
    portal.enqueue = Mock()
    portal.enqueue_job = Mock()
    prewarm = type("Q", (), {"name": "prores_prewarm", "count": 0})()
    prewarm.remove = Mock(return_value=1)

    monkeypatch.setattr(
        queue_jobs, "prores_rq_queue",
        lambda kind: portal if kind == "portal" else prewarm,
    )
    monkeypatch.setattr(queue_jobs, "_live_prewarm_job", lambda *_a, **_k: live)
    monkeypatch.setattr(queue_jobs, "_require_submissions_open", lambda: None)

    job_id = queue_jobs.enqueue_prores_prewarm(
        "job1", "umg_master", force=True, dedupe_live=True,
    )
    assert job_id == live.id
    prewarm.remove.assert_called_once_with(live.id)
    portal.enqueue_job.assert_called_once()
    assert portal.enqueue_job.call_args.kwargs["at_front"] is True
    portal.enqueue.assert_not_called()
    assert live.meta["portal_require_r2_upload"] is True
    assert cap.host_portal_waiting() == 1

    live.origin = "prores_portal"
    live.get_status = lambda refresh=True: "started"
    prewarm.remove.reset_mock()
    portal.enqueue_job.reset_mock()
    again = queue_jobs.enqueue_prores_prewarm(
        "job1", "umg_master", force=True, dedupe_live=True,
    )
    assert again == live.id
    prewarm.remove.assert_not_called()
    portal.enqueue_job.assert_not_called()


def test_promotion_does_not_enqueue_when_the_worker_already_took_the_job(monkeypatch, tmp_path):
    import queue_jobs
    from unittest.mock import Mock

    _dedicated(monkeypatch, tmp_path)
    live = type("Live", (), {})()
    live.id = "prewarm:job1:umg_master"
    live.origin = "prores_prewarm"
    live.meta = {}
    live.get_status = lambda refresh=True: "queued"
    live.save_meta = Mock()
    portal = type("Q", (), {"name": "prores_portal", "count": 0})()
    portal.enqueue = Mock()
    portal.enqueue_job = Mock()
    prewarm = type("Q", (), {"name": "prores_prewarm", "count": 0})()
    prewarm.remove = Mock(return_value=0)
    monkeypatch.setattr(
        queue_jobs, "prores_rq_queue",
        lambda kind: portal if kind == "portal" else prewarm,
    )
    monkeypatch.setattr(queue_jobs, "_live_prewarm_job", lambda *_a, **_k: live)
    monkeypatch.setattr(queue_jobs, "_require_submissions_open", lambda: None)

    job_id = queue_jobs.enqueue_prores_prewarm(
        "job1", "umg_master", force=True, dedupe_live=True,
    )
    assert job_id == live.id
    prewarm.remove.assert_called_once_with(live.id)
    portal.enqueue_job.assert_not_called()
    live.save_meta.assert_not_called()
    assert cap.host_portal_waiting() == 0


def test_download_cap_does_not_leave_rq_retries(monkeypatch, tmp_path):
    import storage

    monkeypatch.setattr(storage, "is_enabled", lambda: True)

    def stalled(key, dest):
        raise cap.DownloadTimeout("total")

    monkeypatch.setattr(cap, "_download_r2", stalled)
    job = type("Job", (), {"id": "portal-1", "retries_left": 2})()
    job.save = lambda: setattr(job, "saved", True)
    monkeypatch.setattr(cap, "_current_rq_job", lambda: job)
    assert cap.download_prores_source("k", str(tmp_path / "source.bin")) is False
    assert job.retries_left == 0
    assert job.saved is True


def test_download_drops_the_portal_marker(monkeypatch, tmp_path):
    _dedicated(monkeypatch, tmp_path)
    cap.mark_portal_demand("portal-prores-abc")
    assert cap.host_portal_waiting() == 1

    class Job:
        id = "portal-prores-abc"

    monkeypatch.setattr(cap, "_current_rq_job", lambda: Job())
    cap.clear_current_portal_demand()
    assert cap.host_portal_waiting() == 0
    portal = (BACKEND / "portal_prores.py").read_text()
    ensure = (BACKEND / "prores.py").read_text().split("def ensure_prores_exists", 1)[1]
    assert portal.index("clear_current_portal_demand()") < portal.index(
        "download_prores_source(source_key, source_path)"
    )
    assert ensure.index("clear_current_portal_demand()") < ensure.index(
        "download_prores_source(source_key, source_path)"
    )


def test_gated_worker_drops_the_intermediate_queue(monkeypatch):
    import worker

    monkeypatch.setenv("PRORES_PORTAL_QUEUE", "prores_portal")
    monkeypatch.setenv("PRORES_PREWARM_QUEUE", "prores_prewarm")
    seen = {}

    def fake_prepare(self, job, remove_from_intermediate_queue=False):
        seen["flag"] = remove_from_intermediate_queue

    monkeypatch.setattr(worker._RQWorker, "prepare_job_execution", fake_prepare)
    host = worker.WarmOnlyWorker.__new__(worker.WarmOnlyWorker)
    host.queues = [type("Q", (), {"name": "prores_portal"})(), type("Q", (), {"name": "prores_prewarm"})()]
    worker.WarmOnlyWorker.prepare_job_execution(
        host, type("Job", (), {"meta": {}})(), False,
    )
    assert seen["flag"] is True

    host.queues = [type("Q", (), {"name": "enterprise"})(), type("Q", (), {"name": "default"})()]
    worker.WarmOnlyWorker.prepare_job_execution(
        host, type("Job", (), {"meta": {}})(), False,
    )
    assert seen["flag"] is False


def test_requeue_drops_the_intermediate_entry_and_restores_demand(monkeypatch, tmp_path):
    import worker

    _dedicated(monkeypatch, tmp_path)
    removed = []

    class Queue:
        name = "prores_portal"
        intermediate_queue_key = "rq:queue:prores_portal:intermediate"

        def __init__(self):
            self.started_job_registry = type("R", (), {"remove": lambda self, job: None})()

        def enqueue_job(self, job, at_front=False):
            removed.append(("enqueue", at_front, job.id))

    class Job:
        id = "portal-prores-1"
        ended_at = "set"
        exc_info = "set"

    host = worker.WarmOnlyWorker.__new__(worker.WarmOnlyWorker)
    host.connection = type("C", (), {"lrem": lambda self, key, n, job_id: removed.append(("lrem", key, job_id))})()
    worker.WarmOnlyWorker._requeue_deferred_prores(
        host, Job(), Queue(), Queue().started_job_registry,
    )
    assert ("lrem", "rq:queue:prores_portal:intermediate", "portal-prores-1") in removed
    assert ("enqueue", True, "portal-prores-1") in removed
    assert cap.host_portal_waiting() == 1


def test_defer_is_not_logged_as_a_job_failure():
    import worker

    host = worker.WarmOnlyWorker.__new__(worker.WarmOnlyWorker)
    called = {}

    def boom(self, job, *exc_info):
        called["super"] = True

    host_cls = worker.WarmOnlyWorker
    # super() looks up the real base. Spy by replacing the RQ method.
    original = worker._RQWorker.handle_exception
    worker._RQWorker.handle_exception = boom
    try:
        exc = cap.ProResDefer("busy")
        host_cls.handle_exception(
            host, type("J", (), {"id": "j"})(), type(exc), exc, None,
        )
    finally:
        worker._RQWorker.handle_exception = original
    assert "super" not in called


def test_host_marker_is_visible_to_the_other_stack(monkeypatch, tmp_path):
    _dedicated(monkeypatch, tmp_path)
    cap.mark_portal_demand("portal-prores-abc")
    assert cap.host_portal_waiting() == 1
    cap.clear_portal_demand("portal-prores-abc")
    assert cap.host_portal_waiting() == 0


def test_marker_is_a_noop_without_dedicated_queues(monkeypatch, tmp_path):
    monkeypatch.delenv("PRORES_PORTAL_QUEUE", raising=False)
    monkeypatch.delenv("PRORES_PREWARM_QUEUE", raising=False)
    monkeypatch.setenv("PRORES_SEMAPHORE_DIR", str(tmp_path))
    cap.mark_portal_demand("portal-prores-abc")
    assert list(tmp_path.rglob("*")) == []


def test_flock_excludes_another_process(monkeypatch, tmp_path):
    _dedicated(monkeypatch, tmp_path)
    monkeypatch.setenv("PRORES_MAX_CONCURRENCY", "1")
    held = cap.try_acquire_slot()
    assert held is not None
    same_process = cap.try_acquire_slot()
    assert same_process is None

    script = textwrap.dedent(
        """
        import os, sys
        sys.path.insert(0, os.environ["BACKEND"])
        from prores_capacity import try_acquire_slot
        slot = try_acquire_slot()
        if slot is None:
            print("busy")
        else:
            print("free")
            slot.release()
        """
    )
    env = os.environ.copy()
    env["BACKEND"] = str(BACKEND)
    env["PRORES_MAX_CONCURRENCY"] = "1"
    env["PRORES_SEMAPHORE_DIR"] = str(tmp_path)
    env["PRORES_SEMAPHORE_NAME"] = "prores"
    blocked = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, check=False,
    )
    assert blocked.returncode == 0, blocked.stderr
    assert blocked.stdout.strip() == "busy"

    env["PRORES_SEMAPHORE_NAME"] = "other-pool"
    other = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, check=False,
    )
    assert other.returncode == 0, other.stderr
    assert other.stdout.strip() == "free"

    held.release()
    env["PRORES_SEMAPHORE_NAME"] = "prores"
    freed = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, check=False,
    )
    assert freed.stdout.strip() == "free"


def test_prewarm_keeps_one_slot_for_portal_when_the_cap_is_above_one(monkeypatch, tmp_path):
    _dedicated(monkeypatch, tmp_path)
    monkeypatch.setenv("PRORES_MAX_CONCURRENCY", "2")
    monkeypatch.delenv("PRORES_PREWARM_MAX_SLOTS", raising=False)
    monkeypatch.delenv("PRORES_PREWARM_BORROW", raising=False)
    assert cap.prewarm_max_slots() == 1
    assert cap.prewarm_may_borrow() is False

    first = cap.try_acquire_slot(for_prewarm=True)
    second = cap.try_acquire_slot(for_prewarm=True)
    assert first is not None
    assert second is None
    # Portal prefers the reserved index, so the shared slot stays usable.
    portal = cap.try_acquire_slot()
    assert portal is not None
    shared = cap.try_acquire_slot(for_prewarm=True)
    assert shared is None
    first.release()
    shared = cap.try_acquire_slot(for_prewarm=True)
    assert shared is not None
    portal.release()
    shared.release()


def test_prewarm_reservation_is_the_same_directory_for_both_stacks(monkeypatch, tmp_path):
    _dedicated(monkeypatch, tmp_path)
    monkeypatch.setenv("PRORES_MAX_CONCURRENCY", "2")
    held = cap.try_acquire_slot(for_prewarm=True)
    script = textwrap.dedent(
        """
        import os, sys
        sys.path.insert(0, os.environ["BACKEND"])
        from prores_capacity import try_acquire_slot
        slot = try_acquire_slot(for_prewarm=os.environ.get("SLOT_FOR") == "prewarm")
        print("busy" if slot is None else "free")
        if slot is not None:
            slot.release()
        """
    )
    env = os.environ.copy()
    env["BACKEND"] = str(BACKEND)
    env["PRORES_MAX_CONCURRENCY"] = "2"
    env["PRORES_SEMAPHORE_DIR"] = str(tmp_path)
    env["PRORES_SEMAPHORE_NAME"] = "prores"
    env["PRORES_PORTAL_QUEUE"] = "prores_portal"
    env["PRORES_PREWARM_QUEUE"] = "prores_prewarm"
    env["SLOT_FOR"] = "prewarm"
    blocked = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, check=False,
    )
    assert blocked.returncode == 0, blocked.stderr
    assert blocked.stdout.strip() == "busy"
    env["SLOT_FOR"] = "portal"
    allowed = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, check=False,
    )
    assert allowed.returncode == 0, allowed.stderr
    assert allowed.stdout.strip() == "free"
    held.release()


def test_prewarm_cap_follows_the_documented_default(monkeypatch):
    monkeypatch.delenv("PRORES_PREWARM_MAX_SLOTS", raising=False)
    monkeypatch.delenv("PRORES_MAX_CONCURRENCY", raising=False)
    assert cap.prewarm_max_slots() is None
    monkeypatch.setenv("PRORES_MAX_CONCURRENCY", "1")
    assert cap.prewarm_max_slots() is None
    monkeypatch.setenv("PRORES_MAX_CONCURRENCY", "4")
    assert cap.prewarm_max_slots() == 3
    monkeypatch.setenv("PRORES_PREWARM_MAX_SLOTS", "0")
    assert cap.prewarm_max_slots() == 0
    monkeypatch.setenv("PRORES_PREWARM_MAX_SLOTS", "nope")
    assert cap.prewarm_max_slots() == 3


def test_prewarm_borrows_the_reserved_slot_only_when_asked_and_idle(monkeypatch, tmp_path):
    _dedicated(monkeypatch, tmp_path)
    monkeypatch.setenv("PRORES_MAX_CONCURRENCY", "2")
    monkeypatch.setenv("PRORES_PREWARM_BORROW", "1")
    first = cap.try_acquire_slot(for_prewarm=True)
    second = cap.try_acquire_slot(for_prewarm=True)
    assert first is not None and second is not None
    first.release()
    second.release()
    cap.mark_portal_demand("portal-waiting")
    only = cap.try_acquire_slot(for_prewarm=True)
    reserved = cap.try_acquire_slot(for_prewarm=True)
    assert only is not None
    assert reserved is None
    portal = cap.try_acquire_slot()
    assert portal is not None
    only.release()
    portal.release()


def test_prewarm_transcode_does_not_take_the_reserved_slot(monkeypatch, tmp_path):
    _dedicated(monkeypatch, tmp_path)
    monkeypatch.setenv("PRORES_MAX_CONCURRENCY", "2")
    monkeypatch.delenv("PRORES_SLOT_HELD", raising=False)
    held = cap.try_acquire_slot(for_prewarm=True)

    class Job:
        id = "prewarm-1"
        origin = "prores_prewarm"

    monkeypatch.setattr(cap, "_current_rq_job", lambda: Job())
    with pytest.raises(cap.ProResDefer):
        with cap.transcode_slot():
            pass
    portal = cap.try_acquire_slot()
    assert portal is not None
    held.release()
    portal.release()


def _gated_worker(monkeypatch, dequeue, *, head_ready, portal_n=1):
    import worker as worker_mod

    monkeypatch.setattr(
        worker_mod, "_queue_length",
        lambda conn, name: portal_n if name == "prores_portal" else 0,
    )
    monkeypatch.setattr(worker_mod, "_head_inputs_ready", lambda *a, **k: head_ready)
    steps = {"n": 0}

    def shutdown(self):
        steps["n"] += 1
        return steps["n"] > 4

    monkeypatch.setattr(worker_mod, "_worker_shutdown_requested", shutdown)
    monkeypatch.setattr(worker_mod, "_sleep_until_shutdown", lambda *a, **k: None)

    class Portal:
        name = "prores_portal"
        job_ids = []
        connection = None
        intermediate_queue_key = "rq:queue:prores_portal:intermediate"

        def __init__(self):
            self.events = []

        def enqueue_job(self, job, at_front=False):
            self.events.append(("enqueue", job.id, at_front))

    portal = Portal()
    prewarm = type("Q", (), {"name": "prores_prewarm", "job_ids": [], "connection": None})()

    class QC:
        @staticmethod
        def dequeue_any(queues, timeout, connection=None, **kwargs):
            return dequeue(queues[0])

    host = worker_mod.WarmOnlyWorker.__new__(worker_mod.WarmOnlyWorker)
    host.__dict__.pop("_prores_slot", None)
    host.queues = [portal, prewarm]
    host.connection = type("C", (), {
        "lrem": lambda self, key, n, job_id: portal.events.append(("lrem", key, job_id)),
    })()
    host.queue_class = QC
    host.job_class = object
    host.serializer = None
    host.death_penalty_class = None
    host.heartbeat = lambda *a, **k: None
    host.reorder_queues = lambda **k: None
    return host, portal


def test_gated_download_reads_a_slot_that_was_never_acquired(monkeypatch, tmp_path):
    _dedicated(monkeypatch, tmp_path)
    monkeypatch.delenv("PRORES_SLOT_HELD", raising=False)
    job = type("Job", (), {"id": "portal-download", "meta": {}})()
    host, portal = _gated_worker(
        monkeypatch, lambda queue: (job, queue), head_ready=False,
    )
    assert "_prores_slot" not in host.__dict__
    got_job, got_queue = host.dequeue_job_and_maintain_ttl(1)
    assert got_job is job
    assert got_queue.name == "prores_portal"
    assert host._prores_slot is None
    assert os.environ.get("PRORES_SLOT_HELD") != "1"
    assert portal.events == []


def test_gated_worker_puts_the_job_back_if_it_fails_after_the_pop(monkeypatch, tmp_path):
    _dedicated(monkeypatch, tmp_path)
    job = type("Job", (), {"id": "portal-stuck", "meta": {}})()

    def boom(ready_job):
        raise RuntimeError("after lmove")

    monkeypatch.setattr(cap, "rq_job_inputs_ready", boom)
    host, portal = _gated_worker(
        monkeypatch, lambda queue: (job, queue), head_ready=False,
    )
    assert host.dequeue_job_and_maintain_ttl(1) is None
    assert ("lrem", "rq:queue:prores_portal:intermediate", "portal-stuck") in portal.events
    assert ("enqueue", "portal-stuck", True) in portal.events


def test_gated_encode_holds_the_slot_until_the_horse_exits(monkeypatch, tmp_path):
    _dedicated(monkeypatch, tmp_path)
    monkeypatch.setenv("PRORES_MAX_CONCURRENCY", "1")
    monkeypatch.delenv("PRORES_SLOT_HELD", raising=False)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"mp4")
    job = type("Job", (), {
        "id": "portal-ready",
        "meta": {"prores_inputs_ready": True, "prores_input_path": str(source)},
    })()
    host, _portal = _gated_worker(
        monkeypatch, lambda queue: (job, queue), head_ready=True,
    )
    got_job, _queue = host.dequeue_job_and_maintain_ttl(1)
    assert got_job is job
    assert host._prores_slot is not None
    assert os.environ.get("PRORES_SLOT_HELD") == "1"
    assert cap.try_acquire_slot() is None
    host._release_prores_slot()
    assert os.environ.get("PRORES_SLOT_HELD") != "1"
    assert cap.try_acquire_slot() is not None


def test_transcode_slot_defers_instead_of_blocking(monkeypatch, tmp_path):
    _dedicated(monkeypatch, tmp_path)
    monkeypatch.setenv("PRORES_MAX_CONCURRENCY", "1")
    monkeypatch.delenv("PRORES_SLOT_HELD", raising=False)
    held = cap.try_acquire_slot()
    with pytest.raises(cap.ProResDefer):
        with cap.transcode_slot():
            pass
    held.release()
    with cap.transcode_slot():
        pass


def test_legacy_queue_ignores_the_cap(monkeypatch):
    monkeypatch.delenv("PRORES_PORTAL_QUEUE", raising=False)
    monkeypatch.delenv("PRORES_PREWARM_QUEUE", raising=False)
    monkeypatch.setenv("PRORES_MAX_CONCURRENCY", "1")
    with cap.transcode_slot():
        pass


def test_copy_bounded_inactivity_and_total():
    def stall(_size, _budget):
        raise TimeoutError()

    with pytest.raises(cap.DownloadTimeout) as inactivity:
        cap.copy_bounded(stall, lambda _chunk: None, inactivity=0.2, total=5)
    assert inactivity.value.reason == "inactivity"

    with pytest.raises(cap.DownloadTimeout) as total:
        cap.copy_bounded(stall, lambda _chunk: None, inactivity=5, total=0.2)
    assert total.value.reason == "total"

    sent = {"n": 0}

    def trickle(_size, _budget):
        sent["n"] += 1
        return b"x"

    with pytest.raises(cap.DownloadTimeout) as slow:
        cap.copy_bounded(trickle, lambda _chunk: None, inactivity=5, total=0.15)
    assert slow.value.reason == "total"
    assert sent["n"] >= 1


def test_run_attempts_retries_only_timeouts():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise cap.DownloadTimeout("total")

    cap.run_attempts(flaky, 3)
    assert calls["n"] == 3

    def always():
        raise cap.DownloadTimeout("inactivity")

    with pytest.raises(cap.DownloadTimeout):
        cap.run_attempts(always, 2)

    def boom():
        raise RuntimeError("auth")

    with pytest.raises(RuntimeError):
        cap.run_attempts(boom, 3)


def test_download_happens_before_the_encode_slot():
    portal = (BACKEND / "portal_prores.py").read_text()
    body = portal.split("def _materialize_body", 1)[1]
    assert body.index("download_prores_source") < body.index("with transcode_slot()")
    assert body.index("with transcode_slot()") < body.index("storage.upload_file")

    ensure = (BACKEND / "prores.py").read_text()
    ensure_body = ensure.split("def ensure_prores_exists", 1)[1].split(
        "def _publish_prores_locked", 1
    )[0]
    publish = ensure.split("def _publish_prores_locked", 1)[1].split(
        "def prewarm_prores", 1
    )[0]
    assert ensure_body.index("download_prores_source") < ensure_body.index(
        "_publish_prores_locked"
    )
    assert publish.index("with transcode_slot()") < publish.index("storage.upload_master")
    prewarm = ensure.split("def prewarm_prores", 1)[1]
    assert "except (ProResMisconfigured, ProResSourceMissing, ProResSuperseded)" in prewarm
    assert "ProResDefer" not in prewarm.split("except", 1)[0]


def test_defer_requeues_at_the_front_without_failing_the_job():
    import worker

    calls = {}

    class Registry:
        def remove(self, job):
            calls["removed"] = job.id

    class Queue:
        name = "prores_portal"

        def __init__(self):
            self.started_job_registry = Registry()

        def enqueue_job(self, job, at_front=False):
            calls["at_front"] = at_front
            calls["job"] = job.id

    class Job:
        id = "portal-prores-1"
        ended_at = "set"
        exc_info = "set"

    host = worker.WarmOnlyWorker.__new__(worker.WarmOnlyWorker)
    queue = Queue()
    result = worker.WarmOnlyWorker.handle_job_failure(
        host, Job(), queue, started_job_registry=queue.started_job_registry,
        exc_string="Traceback (most recent call last):\nProResDefer: encode slot busy\n",
    )
    assert result is None
    assert calls["at_front"] is True
    assert calls["job"] == "portal-prores-1"
    assert calls["removed"] == "portal-prores-1"


def test_whisper_preload_defaults_off(monkeypatch):
    import worker

    monkeypatch.delenv("WHISPER_PRELOAD", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert worker._whisper_preload_enabled() is False
    loaded = {"n": 0}
    assert worker._maybe_preload_whisper(lambda: loaded.__setitem__("n", 1)) == "skipped"
    assert loaded["n"] == 0

    monkeypatch.setenv("WHISPER_PRELOAD", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert worker._maybe_preload_whisper(lambda: loaded.__setitem__("n", 1)) == "skipped_api"
    assert loaded["n"] == 0

    monkeypatch.delenv("OPENAI_API_KEY")
    assert worker._maybe_preload_whisper(lambda: loaded.__setitem__("n", 1)) == "loaded"
    assert loaded["n"] == 1


def test_ovh_layout_separates_prores_from_default():
    script = (REPO / "deploy" / "ovh" / "start-workers.sh").read_text()
    assert "QUEUES=prores_portal,prores_prewarm" in script
    assert "QUEUES=default" in script
    assert "QUEUES=enterprise,canary" in script
    assert "WHISPER_PRELOAD=1 QUEUES=transcription,bg_preview,audio_preview" in script
    assert "WHISPER_PRELOAD=1 WORKLOAD_CLASS=batch QUEUES=campaign_control,transcription_batch" in script
    render = script.split("render)", 1)[1].split(";;", 1)[0]
    assert "QUEUES=enterprise,canary" in render
    assert "QUEUES=default" not in render
    assert "prores_" not in render
    quality = script.split("quality)", 1)[1].split(";;", 1)[0]
    assert "WHISPER_PRELOAD=1" not in quality
    assert "QUEUES=transcription_quality" in quality

    compose = (REPO / "deploy" / "ovh" / "docker-compose.yml").read_text()
    assert "QUEUES: prores_portal,prores_prewarm" in compose
    assert "QUEUES: default" in compose
    assert "WHISPER_PRELOAD: \"1\"" in compose
    assert "/var/lock/genly-prores:/var/lock/genly-prores" in compose

    root = (REPO / "docker-compose.yml").read_text()
    assert "QUEUES=prores_portal,prores_prewarm" in root
    assert "QUEUES=default" in root
    assert "QUEUES=enterprise,canary" in root
    assert "QUEUES=transcription,bg_preview,audio_preview" in root
    assert "WHISPER_PRELOAD=1" in root
