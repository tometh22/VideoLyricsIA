"""Deployment contract for the isolated, bounded quality worker."""

from pathlib import Path
import tomllib


REPO = Path(__file__).resolve().parents[3]


def test_quality_worker_is_one_process_one_replica_and_one_queue():
    path = REPO / "railway" / "quality-worker.toml"
    with path.open("rb") as handle:
        cfg = tomllib.load(handle)

    assert cfg["build"]["dockerfilePath"] == "Dockerfile.worker"
    assert "railway/quality-worker.toml" in cfg["build"]["watchPatterns"]
    deploy = cfg["deploy"]
    assert deploy["numReplicas"] == 1
    assert deploy["restartPolicyType"] == "ON_FAILURE"
    assert deploy["drainingSeconds"] == 1200
    start = deploy["startCommand"]
    assert "require_worker_schema.py" in start
    assert "TRANSCRIPTION_QUALITY_QUEUE_ENABLED=1" in start
    assert "TRANSCRIPTION_QUALITY_MODE=observe" in start
    assert "QUEUES=transcription_quality" in start
    assert "python backend/worker.py" in start
    assert "WorkerPool" not in start
    assert "transcription,bg_preview" not in start
    assert "enterprise" not in start
    assert "default" not in start
