"""Concurrency + idempotency tests for the lazy ProRes flow.

The core invariants for the UMG delivery path:
  - Two parallel callers on the same (job_id, file_type) only fire
    one ffmpeg invocation (the second waits, finds the file, and
    returns it).
  - The transcode writes to .tmp + os.replace, so a partial file
    never appears at the final path.
  - prewarm_prores is a noop when the .mov already exists.
  - prewarm_prores swallows ProResMisconfigured / ProResSourceMissing
    instead of marking the RQ job failed (UMG-not-requested or source
    gone are normal "skip" outcomes).
"""

import os
import threading
from unittest.mock import patch, MagicMock

import pytest

import prores


@pytest.fixture
def fake_outputs(tmp_path, monkeypatch):
    """Point prores.OUTPUTS_DIR at a fresh tmp dir so the test owns the
    filesystem state."""
    monkeypatch.setattr(prores, "OUTPUTS_DIR", str(tmp_path))
    return tmp_path


def _seed_source_mp4(outputs_dir, job_id, source_filename):
    """Create a tiny placeholder source MP4 so the helper passes the
    'source exists locally' check without actually invoking ffmpeg."""
    job_dir = os.path.join(outputs_dir, job_id)
    os.makedirs(job_dir, exist_ok=True)
    src = os.path.join(job_dir, source_filename)
    with open(src, "wb") as f:
        f.write(b"fake-mp4")
    return src


def _job_dict(spec=None):
    return {
        "umg_spec": spec or {"frame_size": "HD", "fps": 24.0, "prores_profile": 3},
        "s3_keys": {},
    }


def test_ensure_prores_skips_when_file_exists(fake_outputs):
    """Fastest path: file already on disk → no ffmpeg, no transcode."""
    job_id = "jobexists"
    file_path = os.path.join(
        fake_outputs, job_id, prores.FILE_MAP_PRORES["umg_master"]
    )
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    with open(file_path, "wb") as f:
        f.write(b"already-there")

    with patch("pipeline._transcode_to_prores") as mock_transcode:
        result = prores.ensure_prores_exists(
            job_id, "umg_master", _job_dict(), tenant_id="t",
        )
    assert result == file_path
    mock_transcode.assert_not_called()


def test_ensure_prores_raises_misconfigured_without_umg_spec(fake_outputs):
    """A non-UMG job must NOT silently transcode — the helper raises so
    the API translates it into a 400."""
    with pytest.raises(prores.ProResMisconfigured):
        prores.ensure_prores_exists(
            "jobnoumg", "umg_master", {"umg_spec": None}, tenant_id="t",
        )


def test_ensure_prores_raises_source_missing(fake_outputs):
    """No source MP4 locally and no R2 key → ProResSourceMissing (404)."""
    with patch("storage.is_enabled", return_value=False):
        with pytest.raises(prores.ProResSourceMissing):
            prores.ensure_prores_exists(
                "jobnosrc", "umg_master", _job_dict(), tenant_id="t",
            )


def test_ensure_prores_writes_via_tmp_and_renames(fake_outputs):
    """Successful transcode writes to .tmp first; os.replace moves it
    to the final path so a parallel caller can never observe a partial
    file at FILE_MAP_PRORES[file_type]."""
    job_id = "jobtmp"
    _seed_source_mp4(fake_outputs, job_id, "lyric_video.mp4")
    final_path = os.path.join(
        fake_outputs, job_id, prores.FILE_MAP_PRORES["umg_master"]
    )
    seen_paths = []

    def fake_transcode(src, dst, spec):
        # Verify the destination is the .tmp path, not the final.
        seen_paths.append(dst)
        assert dst.endswith(".tmp"), f"expected .tmp suffix, got {dst}"
        # Mid-transcode, the final file must NOT exist yet.
        assert not os.path.exists(final_path)
        with open(dst, "wb") as f:
            f.write(b"fake-prores")

    with patch("pipeline._transcode_to_prores", side_effect=fake_transcode):
        with patch("storage.is_enabled", return_value=False):
            result = prores.ensure_prores_exists(
                job_id, "umg_master", _job_dict(), tenant_id="t",
            )
    assert result == final_path
    assert os.path.exists(final_path)
    assert len(seen_paths) == 1
    # .tmp is gone after the rename.
    assert not os.path.exists(seen_paths[0])


def test_ensure_prores_serialises_parallel_callers(fake_outputs):
    """Two threads racing on the same (job_id, file_type) → ffmpeg runs
    once; the loser waits, sees the finished file, and returns it.

    Without the lock+double-check, the previous code would fire two
    parallel ffmpeg processes on the same output path; the validator
    would catch the corruption and one of the requests would 500."""
    job_id = "jobrace"
    _seed_source_mp4(fake_outputs, job_id, "lyric_video.mp4")
    final_path = os.path.join(
        fake_outputs, job_id, prores.FILE_MAP_PRORES["umg_master"]
    )

    transcode_calls = []
    transcode_started = threading.Event()
    transcode_unblock = threading.Event()

    def slow_transcode(src, dst, spec):
        transcode_calls.append(dst)
        transcode_started.set()
        # Hold the lock long enough that the second thread arrives.
        transcode_unblock.wait(timeout=2.0)
        with open(dst, "wb") as f:
            f.write(b"fake-prores")

    results = []

    def caller():
        try:
            r = prores.ensure_prores_exists(
                job_id, "umg_master", _job_dict(), tenant_id="t",
            )
            results.append(r)
        except Exception as e:
            results.append(e)

    with patch("pipeline._transcode_to_prores", side_effect=slow_transcode):
        with patch("storage.is_enabled", return_value=False):
            t1 = threading.Thread(target=caller)
            t2 = threading.Thread(target=caller)
            t1.start()
            # Wait until the first caller is mid-transcode; the second
            # should then queue up on the lock.
            transcode_started.wait(timeout=2.0)
            t2.start()
            transcode_unblock.set()
            t1.join(timeout=5.0)
            t2.join(timeout=5.0)

    assert len(transcode_calls) == 1, (
        f"expected one ffmpeg call under the lock, got {len(transcode_calls)}: "
        f"{transcode_calls}"
    )
    assert results == [final_path, final_path]


def test_prewarm_prores_returns_none_when_job_missing(fake_outputs, monkeypatch):
    """Worker must not raise when the job vanished (deleted) between
    enqueue and execution — RQ would mark it failed."""
    monkeypatch.setattr("jobs.get_job_model", lambda db, job_id: None)
    assert prores.prewarm_prores("ghost", "umg_master") is None


def test_prewarm_prores_swallows_misconfigured(fake_outputs, monkeypatch):
    """Worker logs and returns None when the job has no umg_spec —
    treating "this job isn't UMG" as a normal skip outcome."""
    fake_model = MagicMock()
    fake_model.tenant_id = "t"
    fake_model.to_dict.return_value = {"umg_spec": None, "s3_keys": {}}
    monkeypatch.setattr("jobs.get_job_model", lambda db, job_id: fake_model)
    assert prores.prewarm_prores("nonumgjob", "umg_master") is None
