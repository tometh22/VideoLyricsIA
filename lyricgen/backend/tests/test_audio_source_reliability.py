"""Regression tests for the "audio no disponible" reliability fixes
(2026-06-03, recurring agus.cafisi incident — job 0d05c360895a).

Three layers, pinned here:
  1. storage: input purge is ORPHAN-ONLY (protect every job's input, not a
     fragile per-status allow-list).
  2. pipeline edit re-render: falls back to the rendered deliverable's audio
     when the input is missing, instead of hard-failing.
  (The frontend retry of /source-audio-url lives in App.jsx and is covered by
   the JS test suite.)
"""
import inspect
import storage
import pipeline


def test_active_input_keys_is_orphan_only_not_status_filtered():
    """_active_input_keys must protect EVERY job's input (orphan-only), not a
    per-status allow-list — otherwise a status off the list lets a live job's
    input be purged (the incident)."""
    src = inspect.getsource(storage._active_input_keys)
    # protects all jobs by selecting (tenant_id, job_id) for every row
    assert "db.query(Job.tenant_id, Job.job_id).all()" in src
    # and is NOT filtered by a status allow-list anymore
    assert "Job.status.in_" not in src
    assert '"queued"' not in src and '"done"' not in src


def test_active_input_keys_returns_none_on_db_failure():
    """CRITICAL fail-safe (review 2026-06-03): DB-unreadable must return None
    (distinct from an empty set = 0 jobs), so the caller can ABORT instead of
    treating every input as an orphan and purging live jobs."""
    src = inspect.getsource(storage._active_input_keys)
    # both failure paths (import + query) return None, not set()
    assert src.count("return None") >= 2
    assert "return set()" not in src


def test_cleanup_aborts_when_protected_set_unknown():
    """cleanup_old_inputs must REFUSE to delete when the protect-set is None
    (DB unreadable) — otherwise a transient DB blip mid-sweep purges everything
    (the exact agus.cafisi data-loss the fail-safe prevents)."""
    src = inspect.getsource(storage.cleanup_old_inputs)
    assert "if protected_prefixes is None:" in src
    # aborts with deleted=0 before reaching the delete path
    assert "ABORTED" in src
    abort_idx = src.index("protected_prefixes is None")
    delete_idx = src.index("delete_objects") if "delete_objects" in src else len(src)
    assert abort_idx < delete_idx, "abort guard must come before any delete"


def test_edit_pipeline_recovers_audio_from_deliverable():
    """The edit re-render must NOT hard-fail when the input is gone — it
    extracts the audio from the rendered video/short deliverable."""
    src = inspect.getsource(pipeline.run_edit_pipeline)
    # tries the deliverables in order
    assert 'for _src in ("video", "short")' in src
    # extracts audio via ffmpeg from the downloaded deliverable
    assert "ffmpeg-extract-audio-from-deliverable" in src
    assert "prior_s3_keys" in src
    # only raises after BOTH the input and the deliverable fallback fail
    assert "no recoverable" in src


def test_edit_pipeline_checks_object_exists_before_using_input():
    """Guard against a set-but-DEAD input_r2_key (the lifecycle-purged case):
    object_exists must gate the input download."""
    src = inspect.getsource(pipeline.run_edit_pipeline)
    assert "storage.object_exists(input_r2_key)" in src
