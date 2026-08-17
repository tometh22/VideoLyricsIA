import inspect

import pytest
import rq

import queue_jobs


def test_quality_queue_marker_logs_never_include_exception_messages():
    pending = inspect.getsource(queue_jobs._mark_transcription_quality_pending)
    failed = inspect.getsource(
        queue_jobs._mark_transcription_quality_enqueue_failed,
    )
    assert "job_id, exc" not in pending
    assert "job_id, exc" not in failed
    assert "type(exc).__name__" in pending
    assert "type(exc).__name__" in failed


def test_quality_queue_is_fail_safe_disabled_by_default(monkeypatch):
    monkeypatch.delenv("TRANSCRIPTION_QUALITY_QUEUE_ENABLED", raising=False)
    result = queue_jobs.enqueue_transcription_quality(
        "abc123", expected_revision=2,
        expected_segments_hash="f" * 64, filename="song.wav",
    )
    assert result == "disabled:transcription-quality:abc123"


def test_quality_queue_never_falls_back_to_shared_thread(monkeypatch):
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_QUEUE_ENABLED", "1")
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_ROLLOUT_PERCENT", "100")
    monkeypatch.setattr(queue_jobs, "_require_submissions_open", lambda: None)
    monkeypatch.setattr(queue_jobs, "_init_redis", lambda: (None, None, None))
    monkeypatch.setattr(queue_jobs, "_redis", None)
    with pytest.raises(RuntimeError, match="quality queue unavailable"):
        queue_jobs.enqueue_transcription_quality(
            "abc123", expected_revision=2,
            expected_segments_hash="f" * 64, filename="song.wav",
        )


def test_quality_queue_rollout_is_stable_and_pilot_can_override(monkeypatch):
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_QUEUE_ENABLED", "1")
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_ROLLOUT_PERCENT", "0")
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_PILOT_TENANTS", "pilot-a,pilot-b")
    assert queue_jobs.transcription_quality_rollout_eligible(
        "same-job", "regular",
    ) is False
    assert queue_jobs.transcription_quality_rollout_eligible(
        "same-job", "pilot-a",
    ) is True

    monkeypatch.setenv("TRANSCRIPTION_QUALITY_ROLLOUT_PERCENT", "5")
    first = queue_jobs.transcription_quality_rollout_eligible("same-job", "regular")
    assert queue_jobs.transcription_quality_rollout_eligible(
        "same-job", "regular",
    ) is first
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_ROLLOUT_PERCENT", "100")
    assert queue_jobs.transcription_quality_rollout_eligible(
        "any-job", "regular",
    ) is True


def _quality_queue_ready(monkeypatch):
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_QUEUE_ENABLED", "1")
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_ROLLOUT_PERCENT", "100")
    monkeypatch.setattr(queue_jobs, "_require_submissions_open", lambda: None)
    monkeypatch.setattr(queue_jobs, "_init_redis", lambda: (None, None, None))
    monkeypatch.setattr(queue_jobs, "_redis", object())
    monkeypatch.setattr(queue_jobs, "_active_rq_job", lambda *_args: None)
    monkeypatch.setattr(queue_jobs, "_evict_stale_rq_job", lambda *_args: None)
    monkeypatch.setattr(
        queue_jobs, "_transcription_quality_runtime_token",
        lambda: "v6runtime0000000",
    )


def test_quality_queue_marks_snapshot_before_publish(monkeypatch):
    _quality_queue_ready(monkeypatch)
    events = []
    monkeypatch.setattr(
        queue_jobs, "_mark_transcription_quality_pending",
        lambda *_args, **_kwargs: events.append("pending") or True,
    )

    class FakeQueue:
        def __init__(self, *_args, **_kwargs):
            pass

        def enqueue(self, *_args, **kwargs):
            events.append("published")
            return type("Queued", (), {"id": kwargs["job_id"]})()

    monkeypatch.setattr(rq, "Queue", FakeQueue)
    result = queue_jobs.enqueue_transcription_quality(
        "snapshot", expected_revision=4, expected_segments_hash="a" * 64,
        expected_audio_revision=3, expected_audio_sha256="c" * 64,
    )
    assert result == queue_jobs._transcription_quality_attempt_id(
        "snapshot", expected_revision=4, expected_segments_hash="a" * 64,
        expected_audio_revision=3, expected_audio_sha256="c" * 64,
        runtime_token="v6runtime0000000",
    )
    assert len(result) <= 160
    assert events == ["pending", "published"]


def test_quality_queue_fails_closed_without_authoritative_audio_identity(monkeypatch):
    _quality_queue_ready(monkeypatch)
    result = queue_jobs.enqueue_transcription_quality(
        "legacy", expected_revision=1, expected_segments_hash="a" * 64,
        expected_audio_revision=0, expected_audio_sha256="",
    )
    assert result == "identity-missing:transcription-quality:legacy"


def test_quality_queue_publication_failure_marks_retry_failed(monkeypatch):
    _quality_queue_ready(monkeypatch)
    failures = []
    monkeypatch.setattr(
        queue_jobs, "_mark_transcription_quality_pending",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        queue_jobs, "_mark_transcription_quality_enqueue_failed",
        lambda *args, **kwargs: failures.append((args, kwargs)) or True,
    )

    class FailingQueue:
        def __init__(self, *_args, **_kwargs):
            pass

        def enqueue(self, *_args, **_kwargs):
            raise ConnectionError("redis unavailable")

    monkeypatch.setattr(rq, "Queue", FailingQueue)
    with pytest.raises(ConnectionError):
        queue_jobs.enqueue_transcription_quality(
            "snapshot", expected_revision=4,
            expected_segments_hash="b" * 64,
            expected_audio_revision=5, expected_audio_sha256="d" * 64,
        )
    assert failures and failures[0][0][-1] == "ConnectionError"
    assert failures[0][1] == {
        "expected_audio_revision": 5,
        "expected_audio_sha256": "d" * 64,
    }
