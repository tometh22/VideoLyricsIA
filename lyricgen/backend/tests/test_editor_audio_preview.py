"""Editor audio preview: bounded encoding, cache identity, and dedupe."""

import hashlib
import json
from pathlib import Path

import pytest

import audio_preview
import queue_jobs
import storage


def test_concurrent_preview_enqueue_uses_one_content_addressed_rq_job(monkeypatch):
    class FakeRedis:
        def __init__(self):
            self.value = None

        def set(self, key, value, *, nx=False, ex=None):
            if nx and self.value is not None:
                return False
            self.value = value
            return True

        def eval(self, *args):
            self.value = None
            return 1

    class FakeQueue:
        jobs = []

        def __init__(self, name, connection):
            self.name = name
            self.connection = connection

        def enqueue(self, target, *, args, **kwargs):
            job = type("FakeJob", (), {"id": kwargs["job_id"]})()
            self.jobs.append((self.name, target, args, kwargs))
            return job

    class FakeRetry:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    import sys
    import types
    fake_rq = types.ModuleType("rq")
    fake_rq.Queue = FakeQueue
    fake_rq.Retry = FakeRetry
    monkeypatch.setitem(sys.modules, "rq", fake_rq)
    fake = FakeRedis()
    default_queue = FakeQueue("default", fake)
    monkeypatch.setattr(queue_jobs, "_init_redis", lambda: (fake, default_queue, None))
    monkeypatch.setattr(queue_jobs, "_active_rq_job", lambda *args: None)
    monkeypatch.setattr(queue_jobs, "_evict_stale_rq_job", lambda *args: None)
    digest = "e" * 64
    preview_key = storage.editor_audio_preview_key(digest)

    first = queue_jobs.enqueue_editor_audio_preview(
        "inputs/tenant/job/source.wav", digest, preview_key,
    )
    second = queue_jobs.enqueue_editor_audio_preview(
        "inputs/other/job/source.wav", digest, preview_key,
    )

    assert first["status"] == "queued"
    assert second == {
        "status": "pending",
        "deduplicated": True,
        "job_id": f"editor-audio-preview:{digest}:aac-stereo-96k-v1",
    }
    assert len([job for job in FakeQueue.jobs if job[0] == "audio_preview"]) == 1


def test_preview_lock_is_atomic_for_concurrent_digest_requests():
    class MemoryRedis:
        def __init__(self):
            self.value = None

        def set(self, key, value, *, nx=False, ex=None):
            if nx and self.value is not None:
                return False
            self.value = value
            return True

    redis = MemoryRedis()
    digest = "1" * 64
    first = queue_jobs._acquire_editor_audio_preview_lock(redis, digest)
    second = queue_jobs._acquire_editor_audio_preview_lock(redis, digest)
    assert first and second is None


def test_terminal_preview_failure_does_not_reenqueue_on_every_poll(monkeypatch):
    class FakeRedis:
        def __init__(self):
            self.value = None

        def set(self, key, value, *, nx=False, ex=None):
            if nx and self.value is not None:
                return False
            self.value = value
            return True

        def eval(self, *args):
            self.value = None
            return 1

    class FakeQueue:
        def __init__(self, name, connection):
            self.enqueued = False

        def enqueue(self, *args, **kwargs):
            self.enqueued = True
            raise AssertionError("terminal preview must not be re-enqueued")

    class FailedJob:
        def get_status(self, refresh=True):
            return "failed"

    import sys
    import types
    fake_rq = types.ModuleType("rq")
    fake_rq.Queue = FakeQueue
    fake_rq.Retry = object
    monkeypatch.setitem(sys.modules, "rq", fake_rq)
    fake = FakeRedis()
    monkeypatch.setattr(queue_jobs, "_init_redis", lambda: (fake, FakeQueue("default", fake), None))
    monkeypatch.setattr(queue_jobs, "_existing_rq_job", lambda *args: FailedJob())
    digest = "4" * 64
    result = queue_jobs.enqueue_editor_audio_preview(
        "inputs/tenant/job/source.wav",
        digest,
        storage.editor_audio_preview_key(digest),
    )
    assert result["status"] == "unavailable"
    assert result["deduplicated"] is True


def test_preview_worker_uploads_only_after_duration_and_sha_validation(
    tmp_path, monkeypatch,
):
    digest = hashlib.sha256(b"source").hexdigest()
    preview_key = storage.editor_audio_preview_key(digest)
    uploaded = []

    exists = iter((False, True))
    monkeypatch.setattr(storage, "object_exists", lambda key: next(exists))

    def download(_key, destination):
        Path(destination).write_bytes(b"source")
        return True

    monkeypatch.setattr(storage, "download_object", download)
    monkeypatch.setattr(audio_preview, "_probe_duration", lambda path: 43.0)
    monkeypatch.setattr(audio_preview, "_transcode", lambda src, dst: Path(dst).write_bytes(b"m4a"))
    monkeypatch.setattr(
        storage, "upload_file",
        lambda path, key: uploaded.append((path, key)) or key,
    )
    # The worker imports this helper lazily to avoid a heavyweight import at
    # API startup; patch the module it resolves at runtime.
    import quality_cache
    monkeypatch.setattr(quality_cache, "sha256_file", lambda path: digest)

    result = audio_preview.run_editor_audio_preview_job(
        "inputs/tenant/job/source.wav", digest, preview_key,
    )
    assert result["status"] == "ready"
    assert uploaded and uploaded[0][1] == preview_key


def test_preview_worker_hit_is_idempotent_and_does_not_regenerate(monkeypatch):
    digest = "3" * 64
    preview_key = storage.editor_audio_preview_key(digest)
    monkeypatch.setattr(storage, "object_exists", lambda key: True)
    monkeypatch.setattr(
        storage, "download_object",
        lambda *args: (_ for _ in ()).throw(AssertionError("no source download on hit")),
    )
    monkeypatch.setattr(
        audio_preview, "_transcode",
        lambda *args: (_ for _ in ()).throw(AssertionError("no ffmpeg on hit")),
    )
    assert audio_preview.run_editor_audio_preview_job(
        "inputs/tenant/job/source.wav", digest, preview_key,
    ) == {"status": "exists", "preview_key": preview_key}


def test_preview_worker_rejects_drift_before_r2_upload(monkeypatch):
    digest = "f" * 64
    preview_key = storage.editor_audio_preview_key(digest)
    uploaded = []
    monkeypatch.setattr(storage, "object_exists", lambda key: False)
    monkeypatch.setattr(storage, "download_object", lambda key, dest: Path(dest).write_bytes(b"source") or True)
    monkeypatch.setattr(audio_preview, "_probe_duration", lambda path: 10.0 if path.endswith("source.audio") else 10.06)
    monkeypatch.setattr(audio_preview, "_transcode", lambda src, dst: Path(dst).write_bytes(b"m4a"))
    monkeypatch.setattr(storage, "upload_file", lambda path, key: uploaded.append(key) or key)
    import quality_cache
    monkeypatch.setattr(quality_cache, "sha256_file", lambda path: digest)

    with pytest.raises(RuntimeError, match="duration_drift"):
        audio_preview.run_editor_audio_preview_job(
            "inputs/tenant/job/source.wav", digest, preview_key,
        )
    assert uploaded == []


def test_preview_worker_r2_failure_does_not_touch_original(monkeypatch):
    digest = "2" * 64
    preview_key = storage.editor_audio_preview_key(digest)
    monkeypatch.setattr(storage, "object_exists", lambda key: False)
    monkeypatch.setattr(storage, "download_object", lambda key, dest: Path(dest).write_bytes(b"source") or True)
    monkeypatch.setattr(audio_preview, "_probe_duration", lambda path: 12.0)
    monkeypatch.setattr(audio_preview, "_transcode", lambda src, dst: Path(dst).write_bytes(b"m4a"))
    monkeypatch.setattr(storage, "upload_file", lambda path, key: None)
    import quality_cache
    monkeypatch.setattr(quality_cache, "sha256_file", lambda path: digest)

    with pytest.raises(RuntimeError, match="upload_failed"):
        audio_preview.run_editor_audio_preview_job(
            "inputs/tenant/job/source.wav", digest, preview_key,
        )


def test_preview_failure_never_changes_render_source_contract():
    """The preview module has no render entry point or master mutation path."""
    source = Path(audio_preview.__file__).read_text()
    assert "input_r2_key" in source
    assert "upload_file" in source
    assert "run_pipeline" not in source
    assert "input_r2_key =" not in source


def test_render_pipeline_has_no_preview_dependency():
    import inspect
    import pipeline

    render_source = inspect.getsource(pipeline.run_pipeline)
    assert "input_r2_key" in render_source
    assert "editor_audio_preview" not in render_source
    assert "EDITOR_AUDIO_PREVIEW" not in render_source


def test_r2_cors_allows_range_audio_reads():
    policy_path = Path(__file__).resolve().parents[1] / "scripts" / "r2_cors.json"
    rules = json.loads(policy_path.read_text())["CORSRules"]
    assert any(
        {"GET", "HEAD"}.issubset(set(rule.get("AllowedMethods", [])))
        and "Range" in rule.get("AllowedHeaders", [])
        and "Content-Range" in rule.get("ExposeHeaders", [])
        for rule in rules
    )
