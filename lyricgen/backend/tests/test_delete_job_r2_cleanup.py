"""R2 storage cleanup on job deletion — regression tests.

Two layers of protection:

1. Bug 2 (original): delete_job() and bulk_delete_jobs() removed the DB
   row but left input_r2_key (WAV) and s3_keys (deliverables) in R2
   forever. ~12.5 GB/month leaked.

2. Bug A (2026-05-19): _delete_r2_objects() nuked input_r2_key without
   checking sibling references. Variants and edits share their parent's
   input_r2_key by design (main.py:create_variant). Deleting one failed
   variant broke "Reintentar sin re-subir" on every sibling, including
   the still-alive parent — the next retry 404'd on R2 with the cryptic
   "Could not download source audio from R2" error. Triggered the
   agus.cafisi / Una Vez Más incident on staging.

The fix: _delete_r2_objects(db, job) counts how many other live jobs
share the same input_r2_key. If any do, the audio is preserved (the
last surviving sibling carries the lifetime of the upload). The
cleanup_old_inputs script (30-day retention) reclaims keys that all
referencing jobs have been deleted from.
"""

from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _db_with_no_siblings():
    """MagicMock session whose .query().filter().filter().count() returns 0.

    Use for the common case where the deleted job has no siblings
    referencing its input_r2_key — the audio should be deleted.
    """
    db = MagicMock()
    db.query.return_value.filter.return_value.filter.return_value.count.return_value = 0
    return db


def _db_with_n_siblings(n: int):
    """MagicMock session whose sibling-count query returns n."""
    db = MagicMock()
    db.query.return_value.filter.return_value.filter.return_value.count.return_value = n
    return db


# ---------------------------------------------------------------------------
# Unit: _delete_r2_objects helper
# ---------------------------------------------------------------------------


def test_delete_r2_objects_deletes_input_key(monkeypatch):
    import jobs
    import storage as _st

    deleted = []
    monkeypatch.setattr(_st, "delete_object", lambda k: deleted.append(k))

    class FakeJob:
        job_id = "job1"
        input_r2_key = "inputs/t1/job1/track.wav"
        s3_keys = None

    jobs._delete_r2_objects(_db_with_no_siblings(), FakeJob())
    assert "inputs/t1/job1/track.wav" in deleted


def test_delete_r2_objects_deletes_all_s3_keys(monkeypatch):
    import jobs
    import storage as _st

    deleted = []
    monkeypatch.setattr(_st, "delete_object", lambda k: deleted.append(k))

    class FakeJob:
        job_id = "j"
        input_r2_key = None
        s3_keys = {
            "video": "t1/j/lyric_video.mp4",
            "short": "t1/j/short.mp4",
            "thumbnail": "t1/j/thumbnail.jpg",
            "umg_master": "t1/j/umg_master.mov",
        }

    jobs._delete_r2_objects(_db_with_no_siblings(), FakeJob())
    assert set(deleted) == set(FakeJob.s3_keys.values())


def test_delete_r2_objects_deletes_input_and_s3(monkeypatch):
    import jobs
    import storage as _st

    deleted = []
    monkeypatch.setattr(_st, "delete_object", lambda k: deleted.append(k))

    class FakeJob:
        job_id = "j"
        input_r2_key = "inputs/t/j/audio.wav"
        s3_keys = {"video": "t/j/video.mp4"}

    jobs._delete_r2_objects(_db_with_no_siblings(), FakeJob())
    assert "inputs/t/j/audio.wav" in deleted
    assert "t/j/video.mp4" in deleted


def test_delete_r2_objects_is_best_effort_on_error(monkeypatch):
    """An R2 error must not propagate — the DB delete must still run."""
    import jobs
    import storage as _st

    monkeypatch.setattr(_st, "delete_object", lambda k: (_ for _ in ()).throw(RuntimeError("R2 down")))

    class FakeJob:
        job_id = "j"
        input_r2_key = "inputs/t/j/f.wav"
        s3_keys = {"video": "t/j/video.mp4"}

    # Must not raise
    jobs._delete_r2_objects(_db_with_no_siblings(), FakeJob())


def test_delete_r2_objects_no_op_when_no_keys(monkeypatch):
    import jobs
    import storage as _st

    called = []
    monkeypatch.setattr(_st, "delete_object", lambda k: called.append(k))

    class FakeJob:
        job_id = "j"
        input_r2_key = None
        s3_keys = None

    jobs._delete_r2_objects(_db_with_no_siblings(), FakeJob())
    assert called == []


def test_delete_r2_objects_skips_falsy_s3_values(monkeypatch):
    """None / empty-string values in s3_keys must not be passed to delete_object."""
    import jobs
    import storage as _st

    deleted = []
    monkeypatch.setattr(_st, "delete_object", lambda k: deleted.append(k))

    class FakeJob:
        job_id = "j"
        input_r2_key = None
        s3_keys = {"video": "t/j/video.mp4", "short": None, "thumbnail": ""}

    jobs._delete_r2_objects(_db_with_no_siblings(), FakeJob())
    assert deleted == ["t/j/video.mp4"]


# ---------------------------------------------------------------------------
# Bug A regression: shared input_r2_key must survive sibling deletion
# ---------------------------------------------------------------------------


def test_delete_r2_objects_preserves_input_when_sibling_still_references_it(monkeypatch):
    """Variants share the parent's input_r2_key (main.py:create_variant
    copies parent.input_r2_key to the new job row). Deleting one variant
    must NOT nuke the audio while the parent (or other variants) still
    point at it — "Reintentar sin re-subir" would 404 on all of them.

    Reproduces the agus.cafisi / Una Vez Más incident (2026-05-19):
    operator deleted a failed variant, then a sibling retry crashed
    with "Could not download source audio from R2".
    """
    import jobs
    import storage as _st

    deleted = []
    monkeypatch.setattr(_st, "delete_object", lambda k: deleted.append(k))

    class FakeJob:
        job_id = "variant_that_failed"
        input_r2_key = "inputs/omg/parent_abc/Una_Vez_Mas.wav"
        # Output keys are per-job (no sharing) and remain safe to delete.
        s3_keys = {"video": "variant_that_failed/video.mp4"}

    db = _db_with_n_siblings(1)  # parent (or another variant) still alive
    jobs._delete_r2_objects(db, FakeJob())

    assert "inputs/omg/parent_abc/Una_Vez_Mas.wav" not in deleted, (
        "Shared input_r2_key must be preserved while a sibling references it; "
        "deleting it breaks Reintentar sin re-subir on every sibling job."
    )
    # Per-job outputs still go.
    assert "variant_that_failed/video.mp4" in deleted


def test_delete_r2_objects_deletes_input_when_last_referencer(monkeypatch):
    """The opposite end of Bug A: when no other job references the
    audio anymore, the helper SHOULD delete it. Otherwise inputs would
    leak forever — defeating the original Bug 2 fix.
    """
    import jobs
    import storage as _st

    deleted = []
    monkeypatch.setattr(_st, "delete_object", lambda k: deleted.append(k))

    class FakeJob:
        job_id = "the_only_one"
        input_r2_key = "inputs/t/job/track.wav"
        s3_keys = None

    db = _db_with_n_siblings(0)
    jobs._delete_r2_objects(db, FakeJob())
    assert "inputs/t/job/track.wav" in deleted


def test_delete_r2_objects_keeps_input_when_db_query_throws(monkeypatch):
    """Defense-in-depth: if the sibling-count query fails for any
    reason, the helper must err on the side of keeping the audio.
    Losing the input is worse than leaving an orphan in R2 (the
    cleanup_old_inputs cron reclaims orphans after 30 days; lost
    audio means the operator has to re-upload from scratch).
    """
    import jobs
    import storage as _st

    deleted = []
    monkeypatch.setattr(_st, "delete_object", lambda k: deleted.append(k))

    class FakeJob:
        job_id = "j"
        input_r2_key = "inputs/t/j/audio.wav"
        s3_keys = None

    db = MagicMock()
    db.query.side_effect = RuntimeError("postgres unreachable")

    jobs._delete_r2_objects(db, FakeJob())
    assert "inputs/t/j/audio.wav" not in deleted, (
        "When sibling check fails, the safe default is to preserve the audio."
    )


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
    monkeypatch.setattr(
        jobs, "_delete_r2_objects",
        lambda db, j: cleanup_calls.append(j.job_id),
    )

    job = MagicMock()
    job.job_id = "j-cleanup-test"
    job.tenant_id = "t1"
    job.artist = "Artist"
    job.song_title = "Song"
    job.status = "error"
    db = _mock_db_for_job(job)

    jobs.delete_job(db, "j-cleanup-test", "t1")
    assert "j-cleanup-test" in cleanup_calls, "_delete_r2_objects was not called"


def test_delete_job_does_not_call_r2_cleanup_for_protected_status(monkeypatch):
    import jobs

    cleanup_calls = []
    monkeypatch.setattr(
        jobs, "_delete_r2_objects",
        lambda db, j: cleanup_calls.append(j.job_id),
    )

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
    monkeypatch.setattr(
        jobs, "_delete_r2_objects",
        lambda db, j: cleanup_calls.append(j.job_id),
    )

    def _make_row(job_id, status):
        r = MagicMock(spec=_Job)
        r.job_id = job_id
        r.tenant_id = "t1"
        r.artist = "Artist"
        r.song_title = job_id
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


def test_bulk_delete_archives_paid_veo_before_removing_provenance(
    db, monkeypatch,
):
    import uuid
    from datetime import datetime, timezone

    import jobs
    from cost_attribution import song_key
    from database import AIProvenance, Job, User, VeoBudgetLedger
    from veo_budget import scope_hash

    tenant = f"bulk-ledger-{uuid.uuid4().hex[:8]}"
    user = User(
        username=f"bulk-ledger-user-{uuid.uuid4().hex[:8]}",
        hashed_password="unused",
        tenant_id=tenant,
    )
    db.add(user)
    db.flush()
    rows = [
        Job(job_id="bulkveoled01", user_id=user.id, tenant_id=tenant,
            artist="A", song_title="One", filename="1.mp3", status="error"),
        Job(job_id="bulkveoled02", user_id=user.id, tenant_id=tenant,
            artist="A", song_title="Two", filename="2.mp3", status="queued"),
    ]
    db.add_all(rows)
    db.flush()
    now = datetime.now(timezone.utc)
    for row in rows:
        db.add(AIProvenance(
            job_id=row.job_id, step="video_bg",
            tool_name="veo-3.1-fast-generate-001",
            tool_provider="google_vertex", prompt_sent="paid",
            response_summary="provider_ok", created_at=now,
        ))
    db.commit()
    scopes = {
        scope_hash(tenant, song_key(row.artist, row.song_title, row.job_id))
        for row in rows
    }
    monkeypatch.setattr(jobs, "_delete_r2_objects", lambda *_a, **_k: None)

    try:
        result = jobs.bulk_delete_jobs(
            db, [row.job_id for row in rows], tenant,
        )
        assert set(result["deleted"]) == {row.job_id for row in rows}
        ledger = db.query(VeoBudgetLedger).filter(
            VeoBudgetLedger.scope_hash.in_(scopes),
        ).all()
        assert len(ledger) == 2
        assert db.query(AIProvenance).filter(
            AIProvenance.job_id.in_([row.job_id for row in rows]),
        ).count() == 0
    finally:
        db.query(VeoBudgetLedger).filter(
            VeoBudgetLedger.scope_hash.in_(scopes),
        ).delete(synchronize_session=False)
        db.query(AIProvenance).filter(
            AIProvenance.job_id.in_([row.job_id for row in rows]),
        ).delete(synchronize_session=False)
        db.query(Job).filter(
            Job.job_id.in_([row.job_id for row in rows]),
        ).delete(synchronize_session=False)
        db.query(User).filter(User.id == user.id).delete(
            synchronize_session=False)
        db.commit()
