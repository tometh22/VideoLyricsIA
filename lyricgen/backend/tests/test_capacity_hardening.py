"""Tests for the capacity & concurrency fixes (P1-P5).

These cover the WORLD-CLASS-FOR-UMG hardening:
  P1  — /download is non-blocking when ProRes isn't ready (returns 202).
  P3  — prewarm queue depth backpressure (skip new prewarms when deep).
  P5  — disk capacity gate on /upload (refuse 503 when free disk low).

P2 (outputs cleanup) is covered by `cleanup_old_outputs.py`'s own
internal logic; P4 (DB pool bump) is a static config change with no
behavioural test surface.
"""

import os
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# P1 — /download readiness states (lock-aware short-wait + 202 path)
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_outputs(tmp_path, monkeypatch):
    """Point prores.OUTPUTS_DIR at a fresh tmp dir."""
    import prores
    monkeypatch.setattr(prores, "OUTPUTS_DIR", str(tmp_path))
    return tmp_path


def _job(spec=None, s3_keys=None):
    return {
        "umg_spec": spec or {"frame_size": "HD", "fps": 24.0, "prores_profile": 3},
        "s3_keys": s3_keys or {},
    }


def test_readiness_returns_ready_local_when_file_exists(fake_outputs):
    """Fastest path — .mov already on disk → READY_LOCAL."""
    import prores
    job_id = "ready1"
    final = os.path.join(fake_outputs, job_id, prores.FILE_MAP_PRORES["umg_master"])
    os.makedirs(os.path.dirname(final), exist_ok=True)
    open(final, "wb").close()

    res = prores.check_prores_readiness(job_id, "umg_master", _job(), tenant_id="t")
    assert res.state == prores.ProResReadiness.READY_LOCAL
    assert res.local_path == final


def test_readiness_returns_ready_r2_when_s3_key_present(fake_outputs):
    """No local copy, but s3_keys has it → READY_R2."""
    import prores
    res = prores.check_prores_readiness(
        "r2only", "umg_master",
        _job(s3_keys={"umg_master": "tenant/r2only/umg_master.mov"}),
        tenant_id="t",
    )
    assert res.state == prores.ProResReadiness.READY_R2


def test_readiness_returns_misconfigured_when_no_umg_spec(fake_outputs):
    """Job didn't request UMG → MISCONFIGURED (caller raises 400)."""
    import prores
    res = prores.check_prores_readiness(
        "noumg", "umg_master", {"umg_spec": None}, tenant_id="t",
    )
    assert res.state == prores.ProResReadiness.MISCONFIGURED
    assert res.detail


def test_readiness_returns_source_missing_when_no_mp4_anywhere(fake_outputs):
    """No source MP4 locally and no s3 key for it → SOURCE_MISSING (404)."""
    import prores
    with patch("storage.is_enabled", return_value=False):
        res = prores.check_prores_readiness(
            "nosrc", "umg_master", _job(), tenant_id="t",
        )
    assert res.state == prores.ProResReadiness.SOURCE_MISSING


def test_readiness_returns_not_started_when_source_present_no_lock(fake_outputs):
    """Source MP4 present locally, no lock held, no .mov → NOT_STARTED.
    Caller will enqueue a prewarm and return 202."""
    import prores
    job_id = "queueme"
    job_dir = os.path.join(fake_outputs, job_id)
    os.makedirs(job_dir, exist_ok=True)
    # Create the source MP4 locally so SOURCE_MISSING isn't triggered.
    open(os.path.join(job_dir, "lyric_video.mp4"), "wb").close()

    res = prores.check_prores_readiness(
        job_id, "umg_master", _job(), tenant_id="t",
    )
    assert res.state == prores.ProResReadiness.NOT_STARTED
    assert res.retry_after_seconds == 60


def test_readiness_short_waits_when_lock_held_then_returns_ready(fake_outputs):
    """If a sibling caller is mid-transcode (lock held) and the file
    lands within the short-wait window, return READY_LOCAL — not 202."""
    import threading
    import time as _time
    import prores

    job_id = "midxcode"
    job_dir = os.path.join(fake_outputs, job_id)
    os.makedirs(job_dir, exist_ok=True)
    open(os.path.join(job_dir, "lyric_video.mp4"), "wb").close()
    final = os.path.join(job_dir, prores.FILE_MAP_PRORES["umg_master"])

    lock = prores._prores_lock_for(job_id, "umg_master")
    lock.acquire()
    try:
        # Simulate the transcode finishing 0.3 s later — within the
        # short_wait_seconds=2 we'll pass.
        def _finish_soon():
            _time.sleep(0.3)
            open(final, "wb").close()
            lock.release()
        threading.Thread(target=_finish_soon, daemon=True).start()

        res = prores.check_prores_readiness(
            job_id, "umg_master", _job(), tenant_id="t",
            short_wait_seconds=2.0,
        )
    finally:
        if lock.locked():
            lock.release()

    assert res.state == prores.ProResReadiness.READY_LOCAL
    assert res.local_path == final


def test_readiness_returns_in_progress_when_lock_outlives_short_wait(fake_outputs):
    """Lock held longer than short_wait → IN_PROGRESS (202 + retry)."""
    import prores

    job_id = "slowxcode"
    job_dir = os.path.join(fake_outputs, job_id)
    os.makedirs(job_dir, exist_ok=True)
    open(os.path.join(job_dir, "lyric_video.mp4"), "wb").close()

    lock = prores._prores_lock_for(job_id, "umg_master")
    lock.acquire()
    try:
        res = prores.check_prores_readiness(
            job_id, "umg_master", _job(), tenant_id="t",
            short_wait_seconds=0.2,  # tiny so the test stays fast
        )
    finally:
        lock.release()

    assert res.state == prores.ProResReadiness.IN_PROGRESS
    assert res.retry_after_seconds == 30


# ---------------------------------------------------------------------------
# P3 — prewarm queue backpressure
# ---------------------------------------------------------------------------


def test_prewarm_skips_when_queue_depth_exceeds_threshold(monkeypatch):
    """When the enterprise queue is already full of work, new prewarm
    enqueues skip — the lazy /download path will produce the .mov on
    first click instead, and the queue keeps moving for renders."""
    import queue_jobs

    fake_q_enterprise = MagicMock()
    fake_q_enterprise.count = 99  # way above default threshold of 15
    monkeypatch.setattr(
        queue_jobs, "_init_redis",
        lambda: (object(), object(), fake_q_enterprise),
    )
    before = queue_jobs.prewarm_skipped_total
    result = queue_jobs.enqueue_prores_prewarm("any_job", "umg_master")
    after = queue_jobs.prewarm_skipped_total

    assert result is None
    assert after == before + 1
    fake_q_enterprise.enqueue.assert_not_called()


def test_prewarm_enqueues_when_queue_depth_under_threshold(monkeypatch):
    """Default case: queue is shallow, prewarm enqueues normally."""
    import queue_jobs

    fake_q_enterprise = MagicMock()
    fake_q_enterprise.count = 3
    fake_rq_job = MagicMock()
    fake_rq_job.id = "rq-id-123"
    fake_q_enterprise.enqueue.return_value = fake_rq_job
    monkeypatch.setattr(
        queue_jobs, "_init_redis",
        lambda: (object(), object(), fake_q_enterprise),
    )
    monkeypatch.setattr(queue_jobs, "PRORES_PREWARM_ENABLED", True)
    before = queue_jobs.prewarm_enqueued_total
    result = queue_jobs.enqueue_prores_prewarm("job_ok", "umg_master")
    after = queue_jobs.prewarm_enqueued_total

    assert result == "rq-id-123"
    assert after == before + 1


def test_prewarm_returns_none_when_disabled(monkeypatch):
    import queue_jobs
    monkeypatch.setattr(queue_jobs, "PRORES_PREWARM_ENABLED", False)
    assert queue_jobs.enqueue_prores_prewarm("xx", "umg_master") is None


# ---------------------------------------------------------------------------
# P5 — disk capacity gate on /upload
# ---------------------------------------------------------------------------


def test_disk_gate_raises_503_when_free_below_threshold(monkeypatch):
    """When local disk is below the safety threshold, /upload rejects
    new work with 503 + Retry-After. Better than ENOSPC mid-render."""
    import main
    from fastapi import HTTPException

    fake_du = MagicMock()
    # 2 GB free, threshold 5 GB → must reject.
    fake_du.free = 2 * 1024 * 1024 * 1024
    monkeypatch.setattr(main.shutil, "disk_usage", lambda p: fake_du)
    monkeypatch.setattr(main, "_MIN_FREE_DISK_GB_FOR_UPLOAD", 5.0)

    with pytest.raises(HTTPException) as exc_info:
        main._enforce_disk_capacity()
    assert exc_info.value.status_code == 503
    assert "Retry-After" in (exc_info.value.headers or {})


def test_disk_gate_allows_when_free_above_threshold(monkeypatch):
    """Plenty of disk → no exception, upload proceeds."""
    import main

    fake_du = MagicMock()
    fake_du.free = 50 * 1024 * 1024 * 1024  # 50 GB
    monkeypatch.setattr(main.shutil, "disk_usage", lambda p: fake_du)
    monkeypatch.setattr(main, "_MIN_FREE_DISK_GB_FOR_UPLOAD", 5.0)

    # Should not raise.
    main._enforce_disk_capacity()


def test_disk_gate_silent_on_disk_usage_oserror(monkeypatch):
    """If shutil.disk_usage itself fails (volume unmounted, etc.), the
    gate returns without raising — we don't want to refuse uploads
    because of a metric collection glitch."""
    import main

    def _boom(_p):
        raise OSError("simulated")
    monkeypatch.setattr(main.shutil, "disk_usage", _boom)

    main._enforce_disk_capacity()  # no raise


# ---------------------------------------------------------------------------
# Cleanup of old outputs (P2)
# ---------------------------------------------------------------------------


def test_cleanup_deletes_done_job_with_all_keys_present(tmp_path, monkeypatch):
    """A done job that's fully on R2 + older than the keep-window
    should have its local dir deleted."""
    from scripts import cleanup_old_outputs as co

    monkeypatch.setattr(co, "OUTPUTS_DIR", str(tmp_path))
    monkeypatch.setattr(co, "_KEEP_DONE_MIN", 0)  # immediate eligibility
    monkeypatch.setattr(co, "_DRY_RUN", False)

    job_id = "donejob"
    job_dir = tmp_path / job_id
    job_dir.mkdir()
    (job_dir / "lyric_video.mp4").write_bytes(b"x" * 100)

    fake_model = MagicMock()
    fake_model.to_dict.return_value = {
        "status": "done",
        "delivery_profile": "youtube",
        "files": {"video_url": "/x", "short_url": "/y", "thumbnail_url": "/z"},
        "s3_keys": {"video": "k1", "short": "k2", "thumbnail": "k3"},
    }
    monkeypatch.setattr("jobs.get_job_model", lambda jid: fake_model)

    summary = co.cleanup()
    assert summary["deleted"] == 1
    assert not job_dir.exists()


def test_cleanup_keeps_running_job(tmp_path, monkeypatch):
    """A queued / processing job must NEVER be deleted — even if old."""
    from scripts import cleanup_old_outputs as co

    monkeypatch.setattr(co, "OUTPUTS_DIR", str(tmp_path))
    monkeypatch.setattr(co, "_DRY_RUN", False)

    job_id = "runjob"
    job_dir = tmp_path / job_id
    job_dir.mkdir()

    fake_model = MagicMock()
    fake_model.to_dict.return_value = {
        "status": "processing",
        "delivery_profile": "umg",
        "files": {},
        "s3_keys": {},
    }
    monkeypatch.setattr("jobs.get_job_model", lambda jid: fake_model)

    co.cleanup()
    assert job_dir.exists(), "running jobs must not be cleaned up"


def test_cleanup_deletes_orphan_dir(tmp_path, monkeypatch):
    """A dir with no matching DB row → orphan → delete after grace."""
    from scripts import cleanup_old_outputs as co

    monkeypatch.setattr(co, "OUTPUTS_DIR", str(tmp_path))
    monkeypatch.setattr(co, "_KEEP_ORPHAN_MIN", 0)
    monkeypatch.setattr(co, "_DRY_RUN", False)

    orphan = tmp_path / "ghostjob"
    orphan.mkdir()
    (orphan / "lyric_video.mp4").write_bytes(b"x" * 100)

    monkeypatch.setattr("jobs.get_job_model", lambda jid: None)

    summary = co.cleanup()
    assert summary["deleted"] == 1
    assert not orphan.exists()
