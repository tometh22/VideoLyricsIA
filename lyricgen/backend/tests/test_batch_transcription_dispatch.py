from types import SimpleNamespace

import queue_jobs


def test_batch_outbox_dispatch_preserves_reference_contract(monkeypatch):
    captured = {}

    class FakeQueue:
        def __init__(self, name, connection):
            captured["queue_name"] = name
            captured["connection"] = connection

        def enqueue(self, function, **kwargs):
            captured["function"] = function
            captured["enqueue"] = kwargs
            return SimpleNamespace(id=kwargs["job_id"])

    class FakeRetry:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    fake_redis = object()
    monkeypatch.setattr(queue_jobs, "_require_submissions_open", lambda: None)
    monkeypatch.setattr(
        queue_jobs, "_init_redis", lambda: (fake_redis, object(), None),
    )
    monkeypatch.setattr(queue_jobs, "_redis", fake_redis)

    import rq

    monkeypatch.setattr(rq, "Queue", FakeQueue)
    monkeypatch.setattr(rq, "Retry", FakeRetry)

    event_id = "event-batch-reference"
    queued_id = queue_jobs.enqueue_transcription(
        "job-batch-reference",
        "/tmp/audio.wav",
        language="",
        artist="Artist",
        title="Song",
        filename="song.wav",
        live=True,
        anchor_lyrics="",
        reference_required=True,
        workload_class="batch",
        publication_id=event_id,
        publication_dedupe_key="dedupe-batch-reference",
    )

    assert queued_id == f"transcription:{event_id}"
    assert captured["queue_name"] == "transcription_batch"
    assert captured["connection"] is fake_redis
    target_args = captured["enqueue"]["args"]
    assert target_args[:4] == (
        "job-batch-reference",
        event_id,
        "dedupe-batch-reference",
        "/tmp/audio.wav",
    )
    assert target_args[4]["reference_required"] is True
    assert target_args[4]["workload_class"] == "batch"
