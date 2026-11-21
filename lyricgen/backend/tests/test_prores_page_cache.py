"""Cache advice must never change delivery correctness or run on API paths."""

import errno
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

import file_page_cache as cache
import prores


@pytest.fixture
def render(tmp_path, monkeypatch):
    import delivery_snapshots
    import jobs
    import pipeline
    import storage

    monkeypatch.delenv("PRORES_RELEASE_PAGE_CACHE", raising=False)
    monkeypatch.setattr(prores, "OUTPUTS_DIR", str(tmp_path))
    monkeypatch.setattr(prores, "_render_fingerprint", lambda *_: (0, None, 6, 1))
    monkeypatch.setattr(prores, "_is_superseded", lambda *_: False)
    monkeypatch.setattr(delivery_snapshots, "pin_legacy_deliveries", lambda *_: None)
    monkeypatch.setattr(storage, "is_enabled", lambda: True)
    directory = tmp_path / "job"
    directory.mkdir()
    for name in ("lyric_video.mp4", "short.mp4"):
        (directory / name).write_bytes(b"source")
    events = []

    def transcode(src, dst, spec):
        events.append("transcode")
        Path(dst).write_bytes(b"complete-prores")

    def upload(*_):
        events.append("upload")
        return "test/master.mov"

    def persist(*_):
        events.append("persist")
        return True

    def advise(path, expected):
        assert cache.snapshot_file_identity(path) == expected
        assert Path(path).read_bytes() == b"complete-prores"
        events.append("advise")
        return len(b"complete-prores")

    monkeypatch.setattr(pipeline, "_transcode_to_prores", transcode)
    monkeypatch.setattr(storage, "upload_master", upload)
    monkeypatch.setattr(jobs, "merge_s3_keys", persist)
    monkeypatch.setattr(cache, "release_file_page_cache", advise)
    job = {"umg_spec": {"frame_size": "HD", "fps": 24.0, "prores_profile": 3}}

    def run(worker=True, file_type="umg_master"):
        kwargs = {"release_page_cache": True} if worker else {}
        return prores.ensure_prores_exists("job", file_type, job, "tenant", **kwargs)

    return SimpleNamespace(run=run, events=events, directory=directory, job=job)


@pytest.mark.parametrize("flag,worker", [(None, True), ("0", True), ("typo", True), ("1", False)])
def test_disabled_and_api_paths_never_advise(render, monkeypatch, flag, worker):
    if flag is not None:
        monkeypatch.setenv("PRORES_RELEASE_PAGE_CACHE", flag)
    assert Path(render.run(worker=worker)).read_bytes() == b"complete-prores"
    assert render.events == ["transcode", "upload", "persist"]


@pytest.mark.parametrize("file_type", ["umg_master", "umg_short"])
def test_only_completed_persisted_worker_output_is_advised(render, monkeypatch, file_type):
    monkeypatch.setenv("PRORES_RELEASE_PAGE_CACHE", "true")
    output = render.run(file_type=file_type)
    assert render.events == ["transcode", "upload", "persist", "advise"]
    assert Path(output).read_bytes() == b"complete-prores"
    assert (render.directory / "lyric_video.mp4").read_bytes() == b"source"
    assert (render.directory / "short.mp4").read_bytes() == b"source"


@pytest.mark.parametrize("failure", ["storage_disabled", "upload_none", "upload_error", "persist_false", "persist_error", "identity_unavailable"])
def test_unconfirmed_output_never_advised(render, monkeypatch, failure):
    import jobs
    import storage

    monkeypatch.setenv("PRORES_RELEASE_PAGE_CACHE", "1")

    def fail(*_):
        raise OSError("simulated outage")

    if failure == "storage_disabled":
        monkeypatch.setattr(storage, "is_enabled", lambda: False)
    elif failure.startswith("upload"):
        monkeypatch.setattr(storage, "upload_master", fail if failure == "upload_error" else lambda *_: None)
    elif failure.startswith("persist"):
        monkeypatch.setattr(jobs, "merge_s3_keys", fail if failure == "persist_error" else lambda *_: False)
    else:
        monkeypatch.setattr(cache, "snapshot_file_identity", lambda *_: None)
    assert Path(render.run()).read_bytes() == b"complete-prores"
    assert "advise" not in render.events
    if failure == "identity_unavailable":
        assert render.events == ["transcode", "upload", "persist"]


@pytest.mark.parametrize("during_upload", [False, True])
def test_superseded_render_never_advised(render, monkeypatch, during_upload):
    monkeypatch.setenv("PRORES_RELEASE_PAGE_CACHE", "1")
    checks = iter([False, True] if during_upload else [True])
    monkeypatch.setattr(prores, "_is_superseded", lambda *_: next(checks))
    with pytest.raises(prores.ProResSuperseded):
        render.run()
    assert "advise" not in render.events
    assert "persist" not in render.events


def test_local_cache_hit_does_not_evict_without_new_upload(render, monkeypatch):
    monkeypatch.setenv("PRORES_RELEASE_PAGE_CACHE", "1")
    path = render.directory / "umg_master.mov"
    path.write_bytes(b"existing")
    assert render.run() == str(path)
    assert render.events == []


def test_prewarm_entrypoint_opts_in(render, monkeypatch):
    import jobs

    monkeypatch.setenv("PRORES_RELEASE_PAGE_CACHE", "1")
    monkeypatch.setattr(jobs, "get_job_model", lambda *_: SimpleNamespace(
        tenant_id="tenant", to_dict=lambda: render.job,
    ))
    result = prores.prewarm_prores("job", "umg_master")
    assert Path(result).read_bytes() == b"complete-prores"
    assert render.events == ["transcode", "upload", "persist", "advise"]


def test_parallel_worker_callers_keep_one_complete_output(render, monkeypatch):
    monkeypatch.setenv("PRORES_RELEASE_PAGE_CACHE", "1")
    barrier = threading.Barrier(7, timeout=10)

    def caller(_):
        barrier.wait()
        return render.run()

    with ThreadPoolExecutor(max_workers=7) as pool:
        outputs = list(pool.map(caller, range(7)))
    assert len(set(outputs)) == 1
    assert Path(outputs[0]).read_bytes() == b"complete-prores"
    assert render.events == ["transcode", "upload", "persist", "advise"]


def test_advice_failure_does_not_fail_completed_delivery(render, monkeypatch):
    monkeypatch.setenv("PRORES_RELEASE_PAGE_CACHE", "1")

    def failed_advice(*_):
        raise OSError(errno.EOPNOTSUPP, "unsupported")

    monkeypatch.setattr(cache, "release_file_page_cache", failed_advice)
    assert Path(render.run()).read_bytes() == b"complete-prores"
    assert render.events == ["transcode", "upload", "persist"]


@pytest.mark.parametrize("object_status", ["exists", "missing"])
def test_portal_requires_confirmation_before_advice(render, monkeypatch, object_status):
    import storage

    monkeypatch.setenv("PRORES_RELEASE_PAGE_CACHE", "1")
    monkeypatch.setattr(storage, "object_status", lambda _: object_status)
    monkeypatch.setattr("jobs.get_job_model", lambda *_: SimpleNamespace(
        tenant_id="tenant", to_dict=lambda: render.job,
    ))
    if object_status == "missing":
        with pytest.raises(RuntimeError, match="not confirmed"):
            prores.prewarm_prores("job", "umg_master", require_r2_upload=True)
        assert render.events == ["transcode", "upload"]
        assert (render.directory / "umg_master.mov").read_bytes() == b"complete-prores"
    else:
        path = prores.prewarm_prores("job", "umg_master", require_r2_upload=True)
        assert Path(path).read_bytes() == b"complete-prores"
        assert render.events == ["transcode", "upload", "persist", "advise"]


@pytest.fixture
def advice_file(tmp_path, monkeypatch):
    path = tmp_path / "master.mov"
    path.write_bytes(b"unchanged-deliverable")
    events = []
    monkeypatch.setattr(cache.sys, "platform", "linux")
    monkeypatch.setattr(os, "POSIX_FADV_DONTNEED", 4, raising=False)
    monkeypatch.setattr(os, "fsync", lambda fd: events.append("sync"))
    monkeypatch.setattr(os, "posix_fadvise", lambda *args: events.append("advise"), raising=False)
    return path, cache.snapshot_file_identity(str(path)), events


def test_advice_preserves_file_and_syncs_first(advice_file):
    path, identity, events = advice_file
    assert cache.release_file_page_cache(str(path), identity) == path.stat().st_size
    assert events == ["sync", "advise"]
    assert path.read_bytes() == b"unchanged-deliverable"


@pytest.mark.parametrize("change", ["replaced", "modified", "removed", "symlink", "during_sync"])
def test_advice_skips_changed_files(advice_file, monkeypatch, tmp_path, change):
    path, identity, events = advice_file
    if change == "replaced":
        other = tmp_path / "new.mov"
        other.write_bytes(b"new-file")
        os.replace(other, path)
    elif change == "modified":
        path.write_bytes(b"new-content")
    elif change == "removed":
        path.unlink()
    elif change == "symlink":
        other = tmp_path / "other.mov"
        other.write_bytes(b"do-not-touch")
        path.unlink()
        path.symlink_to(other)
    else:
        monkeypatch.setattr(os, "fsync", lambda fd: path.write_bytes(b"modified-during-sync"))
    assert cache.release_file_page_cache(str(path), identity) == 0
    assert "advise" not in events


@pytest.mark.parametrize("operation", ["fsync", "posix_fadvise"])
def test_os_error_is_nonfatal_and_preserves_file(advice_file, monkeypatch, operation):
    path, identity, events = advice_file

    def unsupported(*_):
        raise OSError(errno.EOPNOTSUPP, "unsupported")

    monkeypatch.setattr(os, operation, unsupported)
    assert cache.release_file_page_cache(str(path), identity) == 0
    assert path.read_bytes() == b"unchanged-deliverable"


@pytest.mark.parametrize("unsupported", ["platform", "function"])
def test_unsupported_platform_is_noop(advice_file, monkeypatch, unsupported):
    path, identity, events = advice_file
    if unsupported == "platform":
        monkeypatch.setattr(cache.sys, "platform", "darwin")
    else:
        monkeypatch.delattr(os, "posix_fadvise")
    assert cache.release_file_page_cache(str(path), identity) == 0
    assert events == []


def test_snapshot_rejects_symlink_directory_and_missing_file(tmp_path):
    path = tmp_path / "link"
    path.symlink_to(tmp_path)
    assert cache.snapshot_file_identity(str(path)) is None
    assert cache.snapshot_file_identity(str(tmp_path)) is None
    assert cache.snapshot_file_identity(str(tmp_path / "missing")) is None
