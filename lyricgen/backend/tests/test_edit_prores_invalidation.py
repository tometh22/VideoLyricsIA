"""Regression tests for the ProRes stale-cache bug (2026-06-09).

Symptom reported by a customer: after approving a video, then editing it and
re-rendering, the MP4 download served the NEW cut but the ProRes (.mov)
download kept serving the PRE-EDIT cut.

Root cause: run_edit_pipeline regenerates lyric_video.mp4 but NOT
umg_master.mov / umg_short.mov (those are lazy-transcoded from the MP4 at
/download time). The edit never invalidated the stale .mov on disk nor the
stale s3_keys["umg_master"] entry, so check_prores_readiness short-circuited
on the old cache.

Two layers of coverage:
  1. Unit test for the jobs.remove_s3_keys helper (the invalidation primitive).
  2. Source-level wiring test that run_edit_pipeline actually invalidates +
     re-warms the ProRes derivatives. Source-level (not a full pipeline run)
     because run_edit_pipeline drives ffmpeg/moviepy end to end; the existing
     edit tests use the same inspect.getsource approach.
"""

import inspect
import os
import uuid

import pytest
from unittest.mock import patch, MagicMock

import prores


def _seed_done_job(tenant_id: str, s3_keys: dict, *, delivery_profile: str = "umg"):
    from database import SessionLocal, Job
    db = SessionLocal()
    try:
        job_id = uuid.uuid4().hex[:12]
        db.add(Job(
            job_id=job_id,
            user_id=1,
            tenant_id=tenant_id,
            artist="Intoxicados",
            song_title="Fuego",
            style="oscuro",
            filename="song.wav",
            status="done",
            current_step="done",
            progress=100,
            delivery_profile=delivery_profile,
            s3_keys=s3_keys,
        ))
        db.commit()
        return job_id
    finally:
        db.close()


def _read_s3_keys(job_id: str) -> dict:
    from database import SessionLocal, Job
    db = SessionLocal()
    try:
        row = db.query(Job).filter(Job.job_id == job_id).first()
        return dict(row.s3_keys or {})
    finally:
        db.close()


def test_remove_s3_keys_drops_only_named_keys(setup_db):
    """remove_s3_keys deletes the ProRes derivatives but leaves the MP4 key
    (and any other deliverable) intact — the MP4 was re-uploaded fresh and
    must keep serving."""
    from jobs import remove_s3_keys

    job_id = _seed_done_job("tenant-a", {
        "video": "tenant-a/job/lyric_video.mp4",
        "short": "tenant-a/job/short.mp4",
        "umg_master": "tenant-a/job/umg_master.mov",
        "umg_short": "tenant-a/job/umg_short.mov",
    })

    assert remove_s3_keys(job_id, ["umg_master", "umg_short"]) is True

    remaining = _read_s3_keys(job_id)
    assert "umg_master" not in remaining
    assert "umg_short" not in remaining
    # MP4 + short survive — the lazy ProRes will re-transcode from the fresh MP4.
    assert remaining["video"] == "tenant-a/job/lyric_video.mp4"
    assert remaining["short"] == "tenant-a/job/short.mp4"


def test_remove_s3_keys_is_noop_when_keys_absent(setup_db):
    """Idempotent: dropping keys that aren't present still returns True and
    leaves the row untouched (e.g. a youtube-only job with no .mov keys)."""
    from jobs import remove_s3_keys

    job_id = _seed_done_job("tenant-b", {"video": "tenant-b/job/lyric_video.mp4"})
    assert remove_s3_keys(job_id, ["umg_master", "umg_short"]) is True
    assert _read_s3_keys(job_id) == {"video": "tenant-b/job/lyric_video.mp4"}


def test_remove_s3_keys_returns_false_for_missing_job(setup_db):
    from jobs import remove_s3_keys
    assert remove_s3_keys("does-not-exist-xyz", ["umg_master"]) is False


def test_edit_pipeline_excludes_prores_from_upload_set():
    """The stale local .mov must NOT be re-uploaded by the edit. The upload
    set passed to _upload_deliverables_to_r2 must exclude the umg URLs."""
    import pipeline
    src = inspect.getsource(pipeline.run_edit_pipeline)
    assert "upload_files" in src, "edit pipeline should build a filtered upload set"
    assert '"umg_master_url"' in src and '"umg_short_url"' in src, (
        "upload set should explicitly exclude the lazy ProRes URLs"
    )
    assert "_upload_deliverables_to_r2(job_id, job_dir, upload_files)" in src, (
        "upload must use the filtered set, not the full files dict"
    )


def test_edit_pipeline_invalidates_and_rewarms_prores():
    """The edit must invalidate the stale ProRes cache (local .mov + s3_keys)
    and re-enqueue the prewarm so the next download serves the fresh cut."""
    import pipeline
    src = inspect.getsource(pipeline.run_edit_pipeline)
    assert "remove_s3_keys(job_id, [\"umg_master\", \"umg_short\"])" in src, (
        "edit pipeline must drop the stale ProRes s3_keys"
    )
    assert "enqueue_prores_prewarm(job_id, \"umg_master\")" in src, (
        "edit pipeline must re-warm the ProRes master after invalidation"
    )
    # The stale local .mov is unlinked so check_prores_readiness step 1
    # (local-disk hit) can't serve it.
    assert "os.unlink(_stale)" in src


def test_edit_pipeline_cancels_inflight_prewarm():
    """The edit must cancel a prewarm still queued from the prior render so it
    can't transcode the stale source and publish over the invalidation."""
    import pipeline
    src = inspect.getsource(pipeline.run_edit_pipeline)
    assert "cancel_rq_job(f\"prewarm:{job_id}:{_ft}\")" in src


def test_run_pipeline_drops_stale_mov_before_rerender():
    """A /retry re-render reuses job_dir; the prior .mov must be dropped at
    the start so the post-render prewarm doesn't short-circuit on it."""
    import pipeline
    src = inspect.getsource(pipeline.run_pipeline)
    assert "umg_master.mov" in src and "dropped stale" in src


# ---------------------------------------------------------------------------
# Freshness fence: a transcode that an edit raced must NOT publish a stale .mov
# (the HIGH/MEDIUM concurrency finding from the adversarial review).
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_outputs(tmp_path, monkeypatch):
    monkeypatch.setattr(prores, "OUTPUTS_DIR", str(tmp_path))
    return tmp_path


def _seed_umg_job(*, edit_count: int = 0):
    from database import SessionLocal, Job
    job_id = uuid.uuid4().hex[:12]
    db = SessionLocal()
    try:
        db.add(Job(
            job_id=job_id, user_id=1, tenant_id="t",
            artist="A", song_title="S", style="oscuro", filename="s.wav",
            status="pending_review", current_step="done", progress=100,
            delivery_profile="umg", edit_count=edit_count,
        ))
        db.commit()
    finally:
        db.close()
    return job_id


def _bump_edit_count(job_id: str):
    from database import SessionLocal, Job
    db = SessionLocal()
    try:
        r = db.query(Job).filter(Job.job_id == job_id).first()
        r.edit_count = (r.edit_count or 0) + 1
        db.commit()
    finally:
        db.close()


def _umg_job_dict():
    return {"umg_spec": {"frame_size": "HD", "fps": 24.0, "prores_profile": 3}, "s3_keys": {}}


def _seed_source(outputs_dir, job_id):
    jd = os.path.join(outputs_dir, job_id)
    os.makedirs(jd, exist_ok=True)
    with open(os.path.join(jd, "lyric_video.mp4"), "wb") as f:
        f.write(b"fake-mp4")


def test_fence_discards_mov_when_edit_races_transcode(setup_db, fake_outputs):
    """If edit_count changes mid-transcode, ensure_prores_exists raises
    ProResSuperseded and does NOT publish the stale .mov (no final file,
    no leftover .tmp)."""
    job_id = _seed_umg_job(edit_count=0)
    _seed_source(fake_outputs, job_id)
    final_path = os.path.join(fake_outputs, job_id, prores.FILE_MAP_PRORES["umg_master"])

    def racing_transcode(src, dst, spec):
        with open(dst, "wb") as f:
            f.write(b"stale-prores")
        # An edit lands while we were transcoding the pre-edit source.
        _bump_edit_count(job_id)

    with patch("pipeline._transcode_to_prores", side_effect=racing_transcode):
        with patch("storage.is_enabled", return_value=False):
            with pytest.raises(prores.ProResSuperseded):
                prores.ensure_prores_exists(job_id, "umg_master", _umg_job_dict(), tenant_id="t")

    assert not os.path.exists(final_path), "stale .mov must NOT be published"
    assert not os.path.exists(final_path + ".tmp"), ".tmp must be cleaned up"


def test_fence_publishes_when_render_unchanged(setup_db, fake_outputs):
    """No edit during the transcode → fingerprint matches → .mov is published
    normally (fence does not over-fire)."""
    job_id = _seed_umg_job(edit_count=1)
    _seed_source(fake_outputs, job_id)
    final_path = os.path.join(fake_outputs, job_id, prores.FILE_MAP_PRORES["umg_master"])

    def clean_transcode(src, dst, spec):
        with open(dst, "wb") as f:
            f.write(b"fresh-prores")

    with patch("pipeline._transcode_to_prores", side_effect=clean_transcode):
        with patch("storage.is_enabled", return_value=False):
            result = prores.ensure_prores_exists(job_id, "umg_master", _umg_job_dict(), tenant_id="t")

    assert result == final_path
    assert os.path.exists(final_path)


def test_prewarm_swallows_superseded(fake_outputs, monkeypatch):
    """A superseded transcode is a normal "no longer current" skip — the RQ
    job must finish, not fail."""
    fake_model = MagicMock()
    fake_model.tenant_id = "t"
    fake_model.to_dict.return_value = _umg_job_dict()
    monkeypatch.setattr("jobs.get_job_model", lambda db, job_id: fake_model)
    monkeypatch.setattr(
        prores, "ensure_prores_exists",
        MagicMock(side_effect=prores.ProResSuperseded("raced")),
    )
    assert prores.prewarm_prores("racedjob", "umg_master") is None


# ---------------------------------------------------------------------------
# Source-identity fence: covers /retry (edit_count reset to 0, no edit signal)
# and the seconds-long R2-upload TOCTOU — the two windows the re-review found.
# ---------------------------------------------------------------------------

def test_mov_is_fresh_helper(tmp_path):
    mov = tmp_path / "m.mov"
    src = tmp_path / "s.mp4"
    mov.write_bytes(b"m")
    src.write_bytes(b"s")
    st = mov.stat().st_mtime_ns
    os.utime(str(src), ns=(st - 1000, st - 1000))   # source older → mov fresh
    assert prores._mov_is_fresh(str(mov), str(src)) is True
    os.utime(str(src), ns=(st + 1000, st + 1000))   # source newer → mov stale
    assert prores._mov_is_fresh(str(mov), str(src)) is False
    # missing source → cannot compare → treat as fresh (status quo)
    assert prores._mov_is_fresh(str(mov), str(tmp_path / "absent.mp4")) is True


def test_fence_fires_on_source_rewrite_without_db_change(setup_db, fake_outputs):
    """The /retry hazard: edit_count/editing_started_at are UNCHANGED across a
    retry, but the source MP4 is re-rendered. The source-identity part of the
    fingerprint must catch it and discard the stale .mov."""
    job_id = _seed_umg_job(edit_count=0)
    _seed_source(fake_outputs, job_id)
    final_path = os.path.join(fake_outputs, job_id, prores.FILE_MAP_PRORES["umg_master"])

    def rewriting_transcode(src, dst, spec):
        with open(dst, "wb") as f:
            f.write(b"stale-prores")
        # Simulate a retry rewriting lyric_video.mp4 in place — different size
        # (8 bytes -> longer) flips the source-identity fingerprint with no DB
        # change at all.
        with open(src, "wb") as f:
            f.write(b"a-freshly-re-rendered-and-longer-mp4-payload")

    with patch("pipeline._transcode_to_prores", side_effect=rewriting_transcode):
        with patch("storage.is_enabled", return_value=False):
            with pytest.raises(prores.ProResSuperseded):
                prores.ensure_prores_exists(job_id, "umg_master", _umg_job_dict(), tenant_id="t")

    assert not os.path.exists(final_path), "stale .mov must NOT be published"


def test_second_fence_skips_merge_when_edit_races_upload(setup_db, fake_outputs):
    """The R2-upload TOCTOU: an edit landing DURING the multi-second upload
    (after os.replace) must not get its stale key merged, and the stale LOCAL
    .mov is dropped. It must NOT delete the R2 object — the key is
    deterministic and a concurrent fresh re-warm may already own it (deleting
    would clobber the fresh master → permanent 404)."""
    job_id = _seed_umg_job(edit_count=0)
    _seed_source(fake_outputs, job_id)
    final_path = os.path.join(fake_outputs, job_id, prores.FILE_MAP_PRORES["umg_master"])

    def clean_transcode(src, dst, spec):
        with open(dst, "wb") as f:
            f.write(b"prores")

    merged, deleted = [], []

    def racing_upload(path, tenant, jid, fn):
        _bump_edit_count(job_id)   # edit lands mid-upload
        return "t/job/umg_master.mov"

    with patch("pipeline._transcode_to_prores", side_effect=clean_transcode), \
         patch("storage.is_enabled", return_value=True), \
         patch("storage.upload_master", side_effect=racing_upload), \
         patch("storage.delete_object", side_effect=lambda k: deleted.append(k)), \
         patch("jobs.merge_s3_keys", side_effect=lambda *a: merged.append(a)):
        with pytest.raises(prores.ProResSuperseded):
            prores.ensure_prores_exists(job_id, "umg_master", _umg_job_dict(), tenant_id="t")

    assert merged == [], "stale key must NOT be merged after a mid-upload edit"
    assert deleted == [], "must NOT delete the deterministic R2 key (would clobber a fresh re-warm)"
    assert not os.path.exists(final_path), "stale local .mov must be dropped"


def test_stale_local_mov_retranscodes_instead_of_serving(setup_db, fake_outputs):
    """A .mov older than its source MP4 (e.g. a pre-deploy straggler) must NOT
    short-circuit; ensure_prores_exists re-transcodes from the fresh source."""
    job_id = _seed_umg_job(edit_count=0)
    jd = os.path.join(fake_outputs, job_id)
    os.makedirs(jd, exist_ok=True)
    final_path = os.path.join(jd, prores.FILE_MAP_PRORES["umg_master"])
    src = os.path.join(jd, "lyric_video.mp4")
    with open(final_path, "wb") as f:
        f.write(b"stale")
    with open(src, "wb") as f:
        f.write(b"newer-source")
    st_mov = os.stat(final_path).st_mtime_ns
    os.utime(src, ns=(st_mov + 10_000, st_mov + 10_000))  # source strictly newer

    transcoded = []

    def fake_transcode(s, d, spec):
        transcoded.append(d)
        with open(d, "wb") as f:
            f.write(b"fresh")

    with patch("pipeline._transcode_to_prores", side_effect=fake_transcode):
        with patch("storage.is_enabled", return_value=False):
            result = prores.ensure_prores_exists(job_id, "umg_master", _umg_job_dict(), tenant_id="t")

    assert transcoded, "a stale local .mov must trigger a re-transcode, not be served"
    assert result == final_path


def test_is_superseded_proceeds_on_db_error(monkeypatch):
    """A transient DB blip during the fence recheck must NOT discard a good
    transcode: _is_superseded returns False ("can't prove superseded")."""
    monkeypatch.setattr(
        prores, "_render_fingerprint",
        MagicMock(side_effect=RuntimeError("postgres unreachable")),
    )
    assert prores._is_superseded("j", "/tmp/x.mp4", (0, None, 1, 2)) is False


def test_is_superseded_true_on_real_change(monkeypatch):
    monkeypatch.setattr(prores, "_render_fingerprint",
                        MagicMock(return_value=(1, None, 9, 9)))
    assert prores._is_superseded("j", "/tmp/x.mp4", (0, None, 1, 2)) is True


def test_retry_cancels_inflight_prewarm():
    """retry_job re-renders from scratch; it must cancel the prior prewarm so
    it can't publish a stale .mov over the cleared s3_keys."""
    import main
    src = inspect.getsource(main.retry_job)
    assert "cancel_rq_job(f\"prewarm:{job_id}:{_ft}\")" in src
