from pathlib import Path
import tomllib


REPO = Path(__file__).resolve().parents[3]


def _config(name: str) -> dict:
    with (REPO / "railway" / name).open("rb") as handle:
        return tomllib.load(handle)


def test_railway_uses_one_config_per_service():
    assert not (REPO / "railway.toml").exists()
    assert {p.name for p in (REPO / "railway").glob("*.toml")} == {
        "api.toml", "worker.toml", "short-worker.toml",
    }


def test_api_deployment_contract():
    cfg = _config("api.toml")
    assert cfg["build"]["dockerfilePath"] == "Dockerfile"
    assert cfg["deploy"]["preDeployCommand"] == [
        "bash /app/backend/scripts/prod_migrate.sh"
    ]
    assert cfg["deploy"]["healthcheckPath"] == "/health/deploy"
    assert cfg["deploy"]["numReplicas"] == 2
    assert cfg["deploy"]["drainingSeconds"] == 1200
    start = cfg["deploy"]["startCommand"]
    assert start.startswith("sh -c ")
    assert 'exec uvicorn main:app' in start
    assert '--port "$PORT"' in start


def test_worker_deployment_contracts_share_image_without_http_healthcheck():
    expected_replicas = {"worker.toml": 7, "short-worker.toml": 3}
    for name, replicas in expected_replicas.items():
        cfg = _config(name)
        assert cfg["build"]["dockerfilePath"] == "Dockerfile.worker"
        assert cfg["deploy"]["startCommand"] == "python backend/worker.py"
        assert cfg["deploy"]["numReplicas"] == replicas
        assert cfg["deploy"]["drainingSeconds"] == 1200
        assert "healthcheckPath" not in cfg["deploy"]


def test_worker_image_contains_backend_and_render_assets():
    dockerfile = (REPO / "lyricgen" / "Dockerfile.worker").read_text()
    assert "COPY backend/ ./backend/" in dockerfile
    assert "COPY assets/ ./assets/" in dockerfile
    assert "rclone.org/install.sh" in dockerfile
    assert 'CMD ["python", "backend/worker.py"]' in dockerfile
    assert "HEALTHCHECK" not in "\n".join(
        line for line in dockerfile.splitlines() if not line.lstrip().startswith("#")
    )


def test_worker_fleet_gate_compares_api_sha_protocol_and_empty_queues():
    from observability import worker_fleet_coherence

    healthy = [
        {"release": "sha-new", "rq_payload_version": 2,
         "queues": ["enterprise", "default"]},
        {"release": "sha-new", "rq_payload_version": 2,
         "queues": ["transcription", "bg_preview"]},
    ]
    assert worker_fleet_coherence(healthy, "sha-new", 2)["coherent"] is True
    assert worker_fleet_coherence(healthy, "sha-api", 2)["coherent"] is False
    assert worker_fleet_coherence(healthy, "sha-new", 3)["coherent"] is False
    assert worker_fleet_coherence(healthy[:1], "sha-new", 2)["missing_queues"] == [
        "bg_preview", "transcription",
    ]


def test_worker_fleet_gate_enforces_expected_service_cardinality():
    from observability import worker_fleet_coherence

    render = {
        "service": "Worker",
        "release": "sha-new",
        "rq_payload_version": 2,
        "queues": ["enterprise", "default"],
    }
    short = {
        "service": "ShortWorker",
        "release": "sha-new",
        "rq_payload_version": 2,
        "queues": ["transcription", "bg_preview"],
    }
    expected = {"worker": 7, "short_worker": 3}

    healthy = worker_fleet_coherence(
        [render] * 7 + [short] * 3, "sha-new", 2, expected
    )
    assert healthy["coherent"] is True
    assert healthy["service_counts"] == {"worker": 7, "short_worker": 3}
    assert healthy["under_replicated"] == {}

    missing_short = worker_fleet_coherence(
        [render] * 7 + [short] * 2, "sha-new", 2, expected
    )
    assert missing_short["coherent"] is False
    assert missing_short["under_replicated"] == {
        "short_worker": {"expected": 3, "actual": 2}
    }


def test_staging_and_production_default_to_strict_7_plus_3(monkeypatch):
    from observability import _fleet_readiness_config

    for name in (
        "FLEET_READINESS_STRICT",
        "EXPECTED_WORKER_REPLICAS",
        "EXPECTED_SHORT_WORKER_REPLICAS",
    ):
        monkeypatch.delenv(name, raising=False)

    assert _fleet_readiness_config("staging") == (
        True, {"worker": 7, "short_worker": 3},
    )
    assert _fleet_readiness_config("production") == (
        True, {"worker": 7, "short_worker": 3},
    )
    assert _fleet_readiness_config("test") == (
        False, {"worker": 0, "short_worker": 0},
    )


def test_staging_strict_gate_can_be_disabled_only_explicitly(monkeypatch):
    from observability import _fleet_readiness_config

    monkeypatch.setenv("FLEET_READINESS_STRICT", "0")
    strict, expected = _fleet_readiness_config("staging")
    assert strict is False
    assert expected == {"worker": 7, "short_worker": 3}
