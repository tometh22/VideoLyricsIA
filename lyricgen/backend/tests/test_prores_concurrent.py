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


# ---------------------------------------------------------------------------
# Pure-recode fast path (world-class UMG flow)
# ---------------------------------------------------------------------------


def _captured_argv(mock_run):
    """Return the ffmpeg argv that subprocess.run was called with —
    skipping the auxiliary ffprobe diagnostic call after the encode."""
    for call in mock_run.call_args_list:
        argv = call.args[0]
        if argv and argv[0] == "ffmpeg":
            return argv
    return None


def test_transcode_uses_pure_recode_when_dims_fps_match(fake_outputs, monkeypatch):
    """When the source MP4 is already at the UMG target dims+fps (the
    output of pipeline.run_pipeline after the world-class refactor),
    _transcode_to_prores must NOT add scale= or fps= filters. Frames
    pass through 1:1 — required to satisfy UMG's manual QC for any of
    the 32 frame-size × fps combos they accept."""
    from unittest.mock import patch
    import pipeline as _pipeline
    from render_spec import RenderSpec

    spec = RenderSpec.umg(frame_size="HD", fps=24.0, prores_profile=3)
    src = "/tmp/source.mp4"
    dst = "/tmp/master.mov"

    # ffprobe says the source already matches the target.
    monkeypatch.setattr(
        _pipeline, "_probe_dims_fps",
        lambda p: (spec.width, spec.height, spec.fps_str),
    )

    with patch.object(_pipeline.subprocess, "run") as mock_run:
        # First call (the transcode) returns rc=0; the diagnostic ffprobe
        # afterwards also returns success.
        ok = MagicMock()
        ok.returncode = 0
        ok.stdout = ""
        ok.stderr = ""
        mock_run.return_value = ok
        # Skip the post-transcode validator (covered separately).
        monkeypatch.setattr(_pipeline, "_validate_umg_master", lambda *_: [])
        # Pretend the output file landed on disk with a non-zero size.
        monkeypatch.setattr(_pipeline.os.path, "exists", lambda p: p == dst)
        monkeypatch.setattr(_pipeline.os.path, "getsize", lambda p: 1_000_000)
        _pipeline._transcode_to_prores(src, dst, spec)

    argv = _captured_argv(mock_run)
    assert argv is not None, "expected an ffmpeg invocation"
    vf_idx = argv.index("-vf")
    vf = argv[vf_idx + 1]
    assert "scale=" not in vf, f"pure-recode must skip scale, got: {vf}"
    assert "fps=" not in vf, f"pure-recode must skip fps filter, got: {vf}"
    # No -r either — the timebase comes from the source.
    assert "-r" not in argv, "pure-recode must not pin -r; source timebase is used"


def test_transcode_falls_back_to_legacy_when_dims_mismatch(fake_outputs, monkeypatch):
    """Old jobs (pre-refactor) had source MP4 at YouTube spec (1080p/24)
    but UMG might still ask for any dims/fps. _transcode_to_prores must
    fall back to the legacy scale+fps path so the call doesn't fail —
    AND must log a warning so operators know the output may fail QC."""
    from unittest.mock import patch
    import pipeline as _pipeline
    from render_spec import RenderSpec

    spec = RenderSpec.umg(frame_size="UHD-4K", fps=60.0, prores_profile=3)
    src = "/tmp/legacy.mp4"
    dst = "/tmp/legacy_master.mov"

    # Source is the old YouTube spec — 1920×1080 @ 24fps.
    monkeypatch.setattr(
        _pipeline, "_probe_dims_fps",
        lambda p: (1920, 1080, "24/1"),
    )

    with patch.object(_pipeline.subprocess, "run") as mock_run:
        ok = MagicMock(); ok.returncode = 0; ok.stdout = ""; ok.stderr = ""
        mock_run.return_value = ok
        monkeypatch.setattr(_pipeline, "_validate_umg_master", lambda *_: [])
        monkeypatch.setattr(_pipeline.os.path, "exists", lambda p: p == dst)
        monkeypatch.setattr(_pipeline.os.path, "getsize", lambda p: 1_000_000)
        _pipeline._transcode_to_prores(src, dst, spec)

    argv = _captured_argv(mock_run)
    assert argv is not None
    vf = argv[argv.index("-vf") + 1]
    # Legacy path keeps the explicit scale+fps for the encode to land
    # on the right dims/timebase.
    assert "scale=3840:2160" in vf
    assert "fps=60" in vf
    assert "-r" in argv  # legacy pins explicit timebase


# ───────────────────────────────────────────────────
# Race: parallel prewarm of umg_master + umg_short
# ───────────────────────────────────────────────────


def test_parallel_prewarm_does_not_overwrite_other_key(fake_outputs, monkeypatch):
    """Reproduces the prod bug fixed 2026-05-12: two prewarm_prores
    invocations running in parallel for the same job (one for umg_master,
    one for umg_short) each receive a STALE snapshot of s3_keys via the
    `job` dict. When each finishes, the buggy code did
    `keys = dict(job["s3_keys"])` then wrote the merged dict back —
    overwriting the OTHER prewarm's key.

    The fix re-reads s3_keys from the DB right before merging. This
    test simulates the race by stubbing get_job_model to return a job
    whose s3_keys has been updated by a sibling prewarm between the
    upload and the merge, and asserts that the second update_job call
    preserves the sibling's key.
    """
    import storage
    import jobs as jobs_module

    job_id = "racejob"
    _seed_source_mp4(fake_outputs, job_id, "lyric_video.mp4")

    # Stale snapshot: caller thinks s3_keys is empty.
    stale_job = {"umg_spec": {"frame_size": "HD", "fps": 24.0, "prores_profile": 3},
                 "s3_keys": {}}

    # By the time our transcode finishes, a sibling prewarm has already
    # added its key. get_job_model needs to return THIS fresh state.
    sibling_added = {"umg_short": "tenant/racejob/umg_short.mov"}

    fresh_model = MagicMock()
    fresh_model.s3_keys = dict(sibling_added)

    captured_keys: dict = {}

    def fake_update_job(job_id_, **kwargs):
        # Capture the final s3_keys value that ensure_prores_exists
        # decided to write — this is the assertion target.
        if "s3_keys" in kwargs:
            captured_keys.update(kwargs["s3_keys"])

    def fake_get_job_model(_db, _job_id):
        return fresh_model

    monkeypatch.setattr(jobs_module, "update_job", fake_update_job)
    monkeypatch.setattr(jobs_module, "get_job_model", fake_get_job_model)
    # storage.upload_master returns the key the helper wrote.
    monkeypatch.setattr(
        storage, "upload_master",
        lambda *_a, **_k: "tenant/racejob/umg_master.mov",
    )
    monkeypatch.setattr(storage, "is_enabled", lambda: True)

    # Fake the transcode itself — we only care about the s3_keys merge.
    def fake_transcode(src, dst, spec):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as f:
            f.write(b"fake-prores")

    import pipeline as _pipeline
    monkeypatch.setattr(_pipeline, "_transcode_to_prores", fake_transcode)

    prores.ensure_prores_exists(job_id, "umg_master", stale_job, tenant_id="tenant")

    # Both keys must survive — ours (umg_master, just written) AND the
    # sibling's (umg_short, written during our transcode).
    assert "umg_master" in captured_keys, (
        "ensure_prores_exists should write umg_master after upload"
    )
    assert "umg_short" in captured_keys, (
        "ensure_prores_exists must NOT wipe the sibling's umg_short — "
        "this is the race the fix exists to prevent"
    )
    assert captured_keys["umg_master"] == "tenant/racejob/umg_master.mov"
    assert captured_keys["umg_short"] == "tenant/racejob/umg_short.mov"
