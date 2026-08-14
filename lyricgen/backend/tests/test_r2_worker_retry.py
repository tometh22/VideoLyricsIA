import pytest


def test_r2_upload_retries_in_same_worker_and_deletes_only_after_success(tmp_path, monkeypatch):
    import jobs
    import pipeline

    local = tmp_path / "lyric_video.mp4"
    local.write_bytes(b"rendered")
    attempts = []
    steps = []

    monkeypatch.setenv("R2_UPLOAD_ATTEMPTS", "3")
    monkeypatch.setattr(pipeline.storage, "is_enabled", lambda: True)
    monkeypatch.setattr(pipeline.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(pipeline.random, "uniform", lambda *_args: 0)
    monkeypatch.setattr(pipeline, "update_job", lambda _job_id, **fields: steps.append(fields))
    monkeypatch.setattr(jobs, "heartbeat", lambda _job_id: None)
    monkeypatch.setattr(jobs, "merge_s3_keys", lambda *_args: None)

    def upload(*_args):
        attempts.append(1)
        if len(attempts) < 3:
            raise OSError("transient R2 outage")
        return "tenant/job/lyric_video.mp4"

    monkeypatch.setattr(pipeline.storage, "upload_master", upload)
    result = pipeline._upload_deliverables_to_r2(
        "retry-job", str(tmp_path), {"video_url": "/download"},
    )

    assert len(attempts) == 3
    assert result == {"video": "tenant/job/lyric_video.mp4"}
    assert steps == [{"current_step": "upload_retry"}, {"current_step": "upload_retry"}]
    assert not local.exists()


def test_r2_upload_exhaustion_preserves_local_output(tmp_path, monkeypatch):
    import jobs
    import pipeline

    local = tmp_path / "lyric_video.mp4"
    local.write_bytes(b"only-copy")
    monkeypatch.setenv("R2_UPLOAD_ATTEMPTS", "3")
    monkeypatch.setattr(pipeline.storage, "is_enabled", lambda: True)
    monkeypatch.setattr(pipeline.storage, "upload_master", lambda *_args: (_ for _ in ()).throw(OSError("down")))
    monkeypatch.setattr(pipeline.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(pipeline.random, "uniform", lambda *_args: 0)
    monkeypatch.setattr(pipeline, "update_job", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(jobs, "heartbeat", lambda _job_id: None)

    with pytest.raises(pipeline.StorageUploadError):
        pipeline._upload_deliverables_to_r2(
            "failed-job", str(tmp_path), {"video_url": "/download"},
        )
    assert local.exists()
    assert local.read_bytes() == b"only-copy"


def test_r2_key_persistence_failure_never_deletes_only_local_copy(tmp_path, monkeypatch):
    import jobs
    import pipeline

    local = tmp_path / "lyric_video.mp4"
    local.write_bytes(b"only-copy")
    monkeypatch.setattr(pipeline.storage, "is_enabled", lambda: True)
    monkeypatch.setattr(
        pipeline.storage, "upload_master",
        lambda *_args: "tenant/job/lyric_video.mp4",
    )
    monkeypatch.setattr(jobs, "heartbeat", lambda _job_id: None)
    monkeypatch.setattr(
        jobs, "merge_s3_keys",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )

    with pytest.raises(pipeline.StorageUploadError, match="key persistence failed"):
        pipeline._upload_deliverables_to_r2(
            "failed-key-job", str(tmp_path), {"video_url": "/download"},
        )
    assert local.exists()


def test_invalid_retry_config_uses_safe_default_and_preserves_output(tmp_path, monkeypatch):
    import jobs
    import pipeline

    local = tmp_path / "lyric_video.mp4"
    local.write_bytes(b"only-copy")
    attempts = []
    monkeypatch.setenv("R2_UPLOAD_ATTEMPTS", "three")
    monkeypatch.setattr(pipeline.storage, "is_enabled", lambda: True)
    monkeypatch.setattr(
        pipeline.storage, "upload_master",
        lambda *_args: attempts.append(1) or (_ for _ in ()).throw(OSError("down")),
    )
    monkeypatch.setattr(pipeline.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(pipeline.random, "uniform", lambda *_args: 0)
    monkeypatch.setattr(pipeline, "update_job", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(jobs, "heartbeat", lambda _job_id: None)

    with pytest.raises(pipeline.StorageUploadError):
        pipeline._upload_deliverables_to_r2(
            "bad-config-job", str(tmp_path), {"video_url": "/download"},
        )
    assert len(attempts) == 3
    assert local.exists()
