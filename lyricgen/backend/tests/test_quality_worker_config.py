"""Deployment contract for the isolated, bounded quality worker."""

from pathlib import Path
import tomllib

from scripts.require_quality_worker_resources import validate_limits


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
    assert "require_quality_worker_resources.py" in start
    assert "TRANSCRIPTION_QUALITY_QUEUE_ENABLED=1" in start
    assert "TRANSCRIPTION_QUALITY_MODE=observe" in start
    # Learning kill switches come from Railway variables and default off in
    # application code; pinning =0 here would make staged activation impossible.
    assert "QUALITY_LEARNING_CAPTURE_ENABLED=" not in start
    assert "QUALITY_LEARNING_MINING_ENABLED=" not in start
    assert "QUALITY_LEARNING_MODEL_SHADOW_ENABLED=" not in start
    assert "QUEUES=transcription_quality" in start
    assert "python backend/worker.py" in start
    assert "WorkerPool" not in start
    assert "transcription,bg_preview" not in start
    assert "enterprise" not in start
    assert "default" not in start


def test_quality_worker_resource_gate_rejects_missing_or_oversized_limits():
    assert validate_limits(
        None, None, max_cpus=2, max_memory_bytes=4 * 1024**3,
    ) == ["cpu_limit_unavailable", "memory_limit_unavailable"]
    errors = validate_limits(
        4, 8 * 1024**3, max_cpus=2, max_memory_bytes=4 * 1024**3,
    )
    assert len(errors) == 2
    assert validate_limits(
        2, 4 * 1024**3, max_cpus=2, max_memory_bytes=4 * 1024**3,
    ) == []
