"""Deployment contract for the isolated, bounded quality worker."""

from pathlib import Path
import tomllib

from scripts.require_quality_worker_resources import validate_limits
from scripts.require_quality_worker_config import connectivity_errors, validate_config


REPO = Path(__file__).resolve().parents[3]


def test_api_defaults_v6_proposals_and_model_off_but_allows_staging_override():
    path = REPO / "railway" / "api.toml"
    with path.open("rb") as handle:
        cfg = tomllib.load(handle)

    start = cfg["deploy"]["startCommand"]
    assert 'QUALITY_V6_PROPOSALS_ENABLED="${QUALITY_V6_PROPOSALS_ENABLED:-0}"' in start
    assert 'QUALITY_V6_MODEL_ENABLED="${QUALITY_V6_MODEL_ENABLED:-0}"' in start


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
    assert "require_quality_worker_config.py" in start
    assert "QUALITY_CONTENT_FINGERPRINT_HMAC_KEY_ID=quality-v6-2026-01" in start
    assert "TRANSCRIPTION_QUALITY_QUEUE_ENABLED=1" in start
    assert "TRANSCRIPTION_QUALITY_MODE=observe" in start
    assert "QUALITY_V6_ANALYSIS_ENABLED=1" in start
    assert "PERFORMANCE_GRAPH_V6_ENABLED=1" in start
    assert "TARGETED_CONSENSUS_ENABLED=1" in start
    assert "TARGETED_ACOUSTIC_STRUCTURE_ENABLED=1" in start
    assert "TARGETED_SLOW_STEM_ENABLED=1" in start
    assert "TARGETED_GEMINI_VERIFY_ENABLED=1" in start
    assert "TARGETED_STRUCTURAL_AUTOREPAIR_MODE=observe" in start
    assert "TARGETED_RESIDUAL_ASR_ENABLED=1" in start
    assert "TARGETED_CONSENSUS_MAX_BILLED_SECONDS=120" in start
    assert 'QUALITY_V6_PROPOSALS_ENABLED="${QUALITY_V6_PROPOSALS_ENABLED:-0}"' in start
    assert 'QUALITY_V6_MODEL_ENABLED="${QUALITY_V6_MODEL_ENABLED:-0}"' in start
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


def test_quality_worker_config_gate_requires_durable_dependencies_and_isolation():
    env = {
        "DATABASE_URL": "postgresql://configured",
        "REDIS_URL": "redis://queue",
        "QUALITY_CACHE_REDIS_URL": "redis://cache",
        "R2_ACCESS_KEY_ID": "configured",
        "R2_SECRET_ACCESS_KEY": "configured",
        "R2_ENDPOINT_URL": "https://configured.invalid",
        "R2_BUCKET": "configured",
        "QUALITY_CONTENT_ATTESTATION_KEY": "quality-test-key-0123456789-ABCDEF",
        "QUALITY_CONTENT_FINGERPRINT_HMAC_KEY_ID": "quality-test-v1",
        "QUEUES": "transcription_quality",
        "TRANSCRIPTION_QUALITY_QUEUE_ENABLED": "1",
        "VOCAL_SEP_ENABLED": "1",
        "REPLICATE_API_TOKEN": "configured",
    }
    assert validate_config(env) == []
    del env["R2_BUCKET"]
    env["QUEUES"] = "transcription,transcription_quality"
    assert validate_config(env) == ["missing_object_bucket", "queue_not_isolated"]


def test_quality_worker_config_gate_accepts_s3_aliases():
    env = {
        "DATABASE_URL": "configured",
        "REDIS_URL": "configured",
        "QUALITY_CACHE_REDIS_URL": "configured",
        "S3_ACCESS_KEY": "configured",
        "S3_SECRET_KEY": "configured",
        "S3_ENDPOINT_URL": "configured",
        "S3_BUCKET": "configured",
        "QUALITY_LEARNING_HMAC_KEY": "quality-test-key-0123456789-ABCDEF",
        "QUALITY_LEARNING_HMAC_KEY_ID": "quality-test-v1",
        "QUEUES": "transcription_quality",
        "TRANSCRIPTION_QUALITY_QUEUE_ENABLED": "true",
        "VOCAL_SEP_ENABLED": "true",
        "REPLICATE_API_TOKEN": "configured",
    }
    assert validate_config(env) == []


def test_quality_worker_connectivity_gate_reports_each_dependency_without_secrets():
    env = {
        "REDIS_URL": "redis://queue-secret",
        "QUALITY_CACHE_REDIS_URL": "redis://cache-secret",
    }
    calls = []

    def redis_probe(url):
        calls.append(url)
        return "queue" in url

    errors = connectivity_errors(
        env, database_probe=lambda: False,
        redis_probe=redis_probe, r2_probe=lambda: False,
        replicate_probe=lambda _token: True,
        openai_probe=lambda _token: True,
    )
    assert errors == [
        "database_unreachable", "quality_cache_unreachable",
        "object_storage_unreachable",
    ]
    assert calls == ["redis://queue-secret", "redis://cache-secret"]
    assert all("secret" not in error for error in errors)


def test_quality_worker_connectivity_checks_enabled_provider_auth_without_cost():
    env = {
        "REDIS_URL": "redis://queue",
        "QUALITY_CACHE_REDIS_URL": "redis://cache",
        "REPLICATE_API_TOKEN": "replicate-secret",
        "OPENAI_API_KEY": "openai-secret",
        "TARGETED_CONSENSUS_ENABLED": "1",
    }
    seen = []
    errors = connectivity_errors(
        env, database_probe=lambda: True, redis_probe=lambda _url: True,
        r2_probe=lambda: True,
        replicate_probe=lambda token: seen.append(("replicate", token)) or False,
        openai_probe=lambda token: seen.append(("openai", token)) or False,
    )
    assert errors == [
        "vocal_separator_provider_unreachable",
        "targeted_asr_provider_unreachable",
    ]
    assert seen == [
        ("replicate", "replicate-secret"), ("openai", "openai-secret"),
    ]


def test_quality_worker_config_gate_requires_provider_for_enabled_consensus():
    env = {
        "DATABASE_URL": "configured", "REDIS_URL": "configured",
        "QUALITY_CACHE_REDIS_URL": "configured",
        "R2_ACCESS_KEY_ID": "configured", "R2_SECRET_ACCESS_KEY": "configured",
        "R2_ENDPOINT_URL": "configured", "R2_BUCKET": "configured",
        "QUEUES": "transcription_quality",
        "TRANSCRIPTION_QUALITY_QUEUE_ENABLED": "1",
        "VOCAL_SEP_ENABLED": "1", "REPLICATE_API_TOKEN": "configured",
        "QUALITY_CONTENT_ATTESTATION_KEY": "quality-test-key-0123456789-ABCDEF",
        "QUALITY_CONTENT_FINGERPRINT_HMAC_KEY_ID": "quality-test-v1",
        "TARGETED_CONSENSUS_ENABLED": "1",
    }
    assert validate_config(env) == ["missing_targeted_asr_provider"]
