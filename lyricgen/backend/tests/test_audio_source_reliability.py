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
