from types import SimpleNamespace
import json

import pytest

import queue_jobs


def test_metadata_less_jobs_are_legacy_v1():
    assert queue_jobs.validate_rq_payload_metadata({}) == 1
    assert queue_jobs.validate_rq_payload_metadata(None) == 1


def test_worker_accepts_current_and_previous_payload_versions():
    current = queue_jobs.RQ_PAYLOAD_VERSION
    assert queue_jobs.validate_rq_payload_metadata(
        {"rq_payload_version": current}
    ) == current
    assert queue_jobs.validate_rq_payload_metadata(
        {"rq_payload_version": current - 1}
    ) == current - 1


@pytest.mark.parametrize("version", [0, 99, True, "not-a-version"])
def test_worker_rejects_unknown_or_malformed_payload_versions(version):
    with pytest.raises(queue_jobs.UnsupportedRQPayloadVersion):
        queue_jobs.validate_rq_payload_metadata({"rq_payload_version": version})


def test_worker_checks_protocol_before_delegating(monkeypatch):
    import worker

    delegated = []
    monkeypatch.setattr(
        worker._RQWorker,
        "prepare_job_execution",
        lambda self, job, remove=False: delegated.append((job, remove)) or True,
    )
    instance = object.__new__(worker.WarmOnlyWorker)

    assert instance.prepare_job_execution(
        SimpleNamespace(meta={"rq_payload_version": queue_jobs.RQ_PAYLOAD_VERSION}),
        True,
    ) is True
    assert len(delegated) == 1

    with pytest.raises(queue_jobs.UnsupportedRQPayloadVersion):
        instance.prepare_job_execution(
            SimpleNamespace(meta={"rq_payload_version": 99}),
            True,
        )
    assert len(delegated) == 1


def test_release_heartbeat_exposes_sha_queues_and_protocol(monkeypatch):
    import worker

    writes = []

    class Connection:
        def setex(self, key, ttl, value):
            writes.append((key, ttl, json.loads(value)))

    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "abc123")
    fake_worker = SimpleNamespace(
        name="render-1",
        queues=[SimpleNamespace(name="enterprise"), SimpleNamespace(name="default")],
        connection=Connection(),
    )

    worker.WarmOnlyWorker._publish_release_heartbeat(fake_worker)

    key, ttl, payload = writes[0]
    assert key == "genly:worker:release:render-1"
    assert ttl >= 30
    assert payload["release"] == "abc123"
    assert payload["queues"] == ["enterprise", "default"]
    assert payload["rq_payload_version"] == queue_jobs.RQ_PAYLOAD_VERSION


def test_legacy_v1_job_adapts_missing_policy_fingerprint(monkeypatch):
    import background_policy
    import rq

    monkeypatch.setattr(rq, "get_current_job", lambda: SimpleNamespace(meta={}))
    assert background_policy.compatible_policy_fingerprint(None, "runtime-sha") == "runtime-sha"


def test_v2_job_does_not_adapt_missing_policy_fingerprint(monkeypatch):
    import background_policy
    import rq

    monkeypatch.setattr(
        rq,
        "get_current_job",
        lambda: SimpleNamespace(meta={"rq_payload_version": queue_jobs.RQ_PAYLOAD_VERSION}),
    )
    assert background_policy.compatible_policy_fingerprint(None, "runtime-sha") is None


def test_v2_job_reads_policy_fingerprint_from_metadata_without_signature_change(monkeypatch):
    import background_policy
    import rq

    monkeypatch.setattr(
        rq,
        "get_current_job",
        lambda: SimpleNamespace(meta={
            "rq_payload_version": queue_jobs.RQ_PAYLOAD_VERSION,
            "background_policy_fingerprint": "background-v5:shadow",
        }),
    )
    assert background_policy.compatible_policy_fingerprint(
        None, "background-v5:off",
    ) == "background-v5:shadow"
