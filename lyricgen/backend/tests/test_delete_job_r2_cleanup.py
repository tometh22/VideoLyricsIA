"""R2 storage cleanup on job deletion — regression tests for Bug 2.

Before the fix: delete_job() and bulk_delete_jobs() removed the DB row
but left input_r2_key (WAV) and s3_keys (deliverables) in R2 forever.
At 250 WAVs/month × ~50 MB each = ~12.5 GB/month of leaked R2 storage.

After the fix: _delete_r2_objects() is called before the DB delete,
attempting storage.delete_object() for every known R2 key, with errors
swallowed so R2 failures never block the DB cleanup.
"""

from unittest.mock import MagicMock, call, patch


# ---------------------------------------------------------------------------
# Unit: _delete_r2_objects helper
# ---------------------------------------------------------------------------


def test_delete_r2_objects_deletes_input_key(monkeypatch):
    import jobs
    import storage as _st

    deleted = []
    monkeypatch.setattr(_st, "delete_object", lambda k: deleted.append(k))

    class FakeJob:
        input_r2_key = "inputs/t1/job1/track.wav"
        s3_keys = None

    jobs._delete_r2_objects(FakeJob())
    assert "inputs/t1/job1/track.wav" in deleted


def test_delete_r2_objects_deletes_all_s3_keys(monkeypatch):
    import jobs
    import storage as _st

    deleted = []
    monkeypatch.setattr(_st, "delete_object", lambda k: deleted.append(k))

    class FakeJob:
        input_r2_key = None
        s3_keys = {
            "video": "t1/j/lyric_video.mp4",
            "short": "t1/j/short.mp4",
            "thumbnail": "t1/j/thumbnail.jpg",
            "umg_master": "t1/j/umg_master.mov",
        }

    jobs._delete_r2_objects(FakeJob())
    assert set(deleted) == set(FakeJob.s3_keys.values())


def test_delete_r2_objects_deletes_input_and_s3(monkeypatch):
    import jobs
    import storage as _st

    deleted = []
    monkeypatch.setattr(_st, "delete_object", lambda k: deleted.append(k))

    class FakeJob:
        input_r2_key = "inputs/t/j/audio.wav"
        s3_keys = {"video": "t/j/video.mp4"}

    jobs._delete_r2_objects(FakeJob())
    assert "inputs/t/j/audio.wav" in deleted
    assert "t/j/video.mp4" in deleted


def test_delete_r2_objects_is_best_effort_on_error(monkeypatch):
    """An R2 error must not propagate — the DB delete must still run."""
    import jobs
    import storage as _st

    monkeypatch.setattr(_st, "delete_object", lambda k: (_ for _ in ()).throw(RuntimeError("R2 down")))

    class FakeJob:
        input_r2_key = "inputs/t/j/f.wav"
        s3_keys = {"video": "t/j/video.mp4"}

    # Must not raise
    jobs._delete_r2_objects(FakeJob())


def test_delete_r2_objects_no_op_when_no_keys(monkeypatch):
    import jobs
    import storage as _st

    called = []
    monkeypatch.setattr(_st, "delete_object", lambda k: called.append(k))

    class FakeJob:
        input_r2_key = None
        s3_keys = None

    jobs._delete_r2_objects(FakeJob())
    assert called == []


def test_delete_r2_objects_skips_falsy_s3_values(monkeypatch):
    """None / empty-string values in s3_keys must not be passed to delete_object."""
    import jobs
    import storage as _st

    deleted = []
    monkeypatch.setattr(_st, "delete_object", lambda k: deleted.append(k))

    class FakeJob:
        input_r2_key = None
        s3_keys = {"video": "t/j/video.mp4", "short": None, "thumbnail": ""}

    jobs._delete_r2_objects(FakeJob())
    assert deleted == ["t/j/video.mp4"]


# ---------------------------------------------------------------------------
# Unit: delete_job calls _delete_r2_objects before DB deletion
# ---------------------------------------------------------------------------


def _mock_db_for_job(job_mock):
    """Return a MagicMock session that yields job_mock on query().filter().first()."""
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = job_mock
    return db


def test_delete_job_calls_r2_cleanup(monkeypatch):
    import jobs

    cleanup_calls = []
    monkeypatch.setattr(jobs, "_delete_r2_objects", lambda j: cleanup_calls.append(j.job_id))

    job = MagicMock()
    job.job_id = "j-cleanup-test"
    job.tenant_id = "t1"
    job.status = "error"
    db = _mock_db_for_job(job)

    jobs.delete_job(db, "j-cleanup-test", "t1")
    assert "j-cleanup-test" in cleanup_calls, "_delete_r2_objects was not called"


def test_delete_job_does_not_call_r2_cleanup_for_protected_status(monkeypatch):
    import jobs

    cleanup_calls = []
    monkeypatch.setattr(jobs, "_delete_r2_objects", lambda j: cleanup_calls.append(j.job_id))

    job = MagicMock()
    job.job_id = "j-done"
    job.tenant_id = "t1"
    job.status = "done"
    db = _mock_db_for_job(job)

    ok, reason = jobs.delete_job(db, "j-done", "t1")
    assert not ok
    assert cleanup_calls == [], "R2 cleanup must not run for protected jobs"


# ---------------------------------------------------------------------------
# Unit: bulk_delete_jobs calls _delete_r2_objects for each deletable job
# ---------------------------------------------------------------------------


def test_bulk_delete_jobs_cleans_all_deletable(monkeypatch):
    import jobs
    from database import Job as _Job

    cleanup_calls = []
    monkeypatch.setattr(jobs, "_delete_r2_objects", lambda j: cleanup_calls.append(j.job_id))

    def _make_row(job_id, status):
        r = MagicMock(spec=_Job)
        r.job_id = job_id
        r.status = status
        r.input_r2_key = f"inputs/{job_id}/track.wav"
        r.s3_keys = {}
        return r

    rows = [_make_row("j1", "error"), _make_row("j2", "error"), _make_row("j3", "done")]
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = rows
    db.query.return_value.filter.return_value.delete.return_value = None

    result = jobs.bulk_delete_jobs(db, ["j1", "j2", "j3"], "t1")
    assert set(result["deleted"]) == {"j1", "j2"}
    # R2 cleanup only for deletable rows (j1, j2), not protected (j3)
    assert set(cleanup_calls) == {"j1", "j2"}
    assert "j3" not in cleanup_calls
