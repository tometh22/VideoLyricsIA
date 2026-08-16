from pathlib import Path
import tomllib


REPO = Path(__file__).resolve().parents[3]


def _config(name: str) -> dict:
    with (REPO / "railway" / name).open("rb") as handle:
        return tomllib.load(handle)


def test_ci_runs_for_stacked_pull_requests():
    workflow = (REPO / ".github" / "workflows" / "ci.yml").read_text()
    pull_request_block = workflow.split("pull_request:", 1)[1].split("jobs:", 1)[0]
    assert "branches:" not in pull_request_block, (
        "Stacked PRs must run CI even when their temporary base is another "
        "feature branch."
    )


def test_railway_uses_one_config_per_service():
    assert not (REPO / "railway.toml").exists()
    assert {p.name for p in (REPO / "railway").glob("*.toml")} == {
        "api.toml", "worker.toml", "short-worker.toml", "quality-worker.toml",
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
    expected_start = (
        "sh -c 'python backend/scripts/require_worker_schema.py "
        "&& exec python backend/worker.py'"
    )
    for name, replicas in expected_replicas.items():
        cfg = _config(name)
        assert cfg["build"]["dockerfilePath"] == "Dockerfile.worker"
        assert cfg["deploy"]["startCommand"] == expected_start
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


def test_quality_producer_requires_an_isolated_quality_consumer(monkeypatch):
    from observability import _fleet_readiness_config, worker_fleet_coherence

    monkeypatch.setenv("TRANSCRIPTION_QUALITY_QUEUE_ENABLED", "1")
    monkeypatch.delenv("EXPECTED_QUALITY_WORKER_REPLICAS", raising=False)
    strict, expected = _fleet_readiness_config("staging")
    assert strict is True
    assert expected["quality_worker"] == 1

    workers = [
        {"service": "Worker", "release": "sha", "rq_payload_version": 2,
         "queues": ["enterprise", "default"]},
        {"service": "ShortWorker", "release": "sha", "rq_payload_version": 2,
         "queues": ["transcription", "bg_preview"]},
    ]
    missing = worker_fleet_coherence(workers, "sha", 2, expected)
    assert missing["coherent"] is False
    assert missing["missing_queues"] == ["transcription_quality"]
    assert missing["under_replicated"]["quality_worker"]["actual"] == 0

    workers.append({
        "service": "QualityWorker", "release": "sha", "rq_payload_version": 2,
        "queues": ["transcription_quality"],
    })
    assert worker_fleet_coherence(workers, "sha", 2, expected)["coherent"] is False
    # This compact fixture has one render and short worker, while staging's
    # normal defaults require 7+3; isolate the queue contract itself.
    expected = {"worker": 1, "short_worker": 1, "quality_worker": 1}
    assert worker_fleet_coherence(workers, "sha", 2, expected)["coherent"] is True


def test_frontend_only_deploy_no_marca_la_flota_como_incoherente():
    """El SHA de git no sirve para comparar API vs workers.

    Los servicios de worker tienen filtros de path en Railway: un commit que
    sólo toca el frontend NO los redeploya — correctamente, no hay nada nuevo
    que correr ahí. Pero el SHA de la API sí avanza. Con la comparación por SHA,
    /health quedaba en `down` después de CADA merge de sólo-frontend, con la
    base arriba, Redis arriba, los workers vivos y las colas vacías.

    Observado en staging el 25-jul-2026: api en 1e72313e (PR frontend-only),
    workers en fc44880e (el último commit que tocó backend), deploy del Worker
    marcado SKIPPED por Railway. Todo sano, health "down".

    Una alarma que grita en falso de rutina enseña a ignorarla — y el día que un
    worker quede atrás de verdad, nadie mira.
    """
    from observability import worker_fleet_coherence

    workers = [
        {"release": "sha-backend", "code_fingerprint": "abc123",
         "rq_payload_version": 2, "queues": ["enterprise", "default"]},
        {"release": "sha-backend", "code_fingerprint": "abc123",
         "rq_payload_version": 2, "queues": ["transcription", "bg_preview"]},
    ]
    # API un commit más adelante, pero MISMO código de backend.
    out = worker_fleet_coherence(
        workers, "sha-frontend-only", 2, api_code_fingerprint="abc123",
    )
    assert out["release_match"] is True
    assert out["coherent"] is True


def test_pero_un_worker_con_backend_VIEJO_sigue_marcandose():
    """No se pierde la señal real: si el código de backend difiere, se avisa."""
    from observability import worker_fleet_coherence

    workers = [
        {"release": "sha-old", "code_fingerprint": "viejo999",
         "rq_payload_version": 2, "queues": ["enterprise", "default"]},
        {"release": "sha-old", "code_fingerprint": "viejo999",
         "rq_payload_version": 2, "queues": ["transcription", "bg_preview"]},
    ]
    out = worker_fleet_coherence(
        workers, "sha-new", 2, api_code_fingerprint="nuevo111",
    )
    assert out["release_match"] is False
    assert out["coherent"] is False


def test_worker_sin_huella_cae_al_SHA_sin_ventana_ciega():
    """Durante el rollout los workers viejos todavía no publican la huella.

    Si en ese caso el gate diera coherente por defecto, habría una ventana en la
    que un worker realmente desactualizado pasaría desapercibido. Cae al
    comportamiento de siempre: comparar el SHA.
    """
    from observability import worker_fleet_coherence

    sin_huella = [
        {"release": "sha-old", "rq_payload_version": 2,
         "queues": ["enterprise", "default"]},
        {"release": "sha-old", "rq_payload_version": 2,
         "queues": ["transcription", "bg_preview"]},
    ]
    assert worker_fleet_coherence(
        sin_huella, "sha-new", 2, api_code_fingerprint="nuevo111",
    )["release_match"] is False
    assert worker_fleet_coherence(
        sin_huella, "sha-old", 2, api_code_fingerprint="nuevo111",
    )["release_match"] is True


def test_el_protocolo_sigue_siendo_bloqueante_aunque_la_huella_coincida():
    """La huella no puede tapar una incompatibilidad de payload de la cola."""
    from observability import worker_fleet_coherence

    workers = [
        {"release": "sha", "code_fingerprint": "abc123",
         "rq_payload_version": 1, "queues": ["enterprise", "default"]},
        {"release": "sha", "code_fingerprint": "abc123",
         "rq_payload_version": 1, "queues": ["transcription", "bg_preview"]},
    ]
    out = worker_fleet_coherence(workers, "sha", 2, api_code_fingerprint="abc123")
    assert out["protocol_match"] is False
    assert out["coherent"] is False


def test_la_huella_es_estable_y_no_incluye_los_tests():
    """Si la huella cambiara entre llamadas, el gate sería un generador de
    ruido. Y si incluyera tests/, tocar un test marcaría la flota incoherente."""
    from observability import backend_code_fingerprint

    a = backend_code_fingerprint()
    b = backend_code_fingerprint()
    assert a == b
    assert a and len(a) == 16
