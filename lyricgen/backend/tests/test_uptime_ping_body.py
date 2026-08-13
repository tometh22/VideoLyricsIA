"""El uptime ping debe leer el cuerpo COMPLETO de /health.

Regresión real (2026-07-25): `_probe` leía 2048 bytes fijos. El payload de
/health fue creciendo (fleet_*, worker_releases, r2, reaper, submissions…)
hasta pasar ese corte, así que `json.loads` recibía JSON truncado y el monitor
reportaba "prod caído" con producción respondiendo 200 / status=ok.

El falso positivo no era inofensivo: dejaba en rojo el check suite de `main`, y
Railway —configurado para esperar el check suite— **salteaba los deploys de
producción** ("CI check suite failed"). Peor: como un rollout parcial deja la
flota incoherente y eso hace que /health devuelva 503, el círculo se
retroalimentaba y solo se salía con un Redeploy manual del dashboard.
"""

import io
import json

from scripts.preflight import uptime_ping


class _FakeResponse(io.BytesIO):
    """Responde como urlopen(): context manager + getcode() + read(n)."""

    def __init__(self, payload: bytes, code: int = 200):
        super().__init__(payload)
        self._code = code

    def getcode(self):
        return self._code

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _health_payload(extra_bytes: int = 0) -> dict:
    """Un /health realista, con relleno para superar cualquier tope de lectura."""
    payload = {
        "status": "ok",
        "env": "production",
        "release": "b2ba27ff6217c4e59c2dd55ea08eac19c69c4ce3",
        "db": "up",
        "redis": "up",
        "fleet_coherent": True,
        "fleet_release_match": True,
        "worker_releases": [],
    }
    if extra_bytes:
        payload["worker_releases"] = [
            {"service": "Worker", "worker": "w" * 32, "release": "b" * 40}
            for _ in range(max(1, extra_bytes // 96))
        ]
    return payload


def _patch_urlopen(monkeypatch, payload: dict, code: int = 200):
    body = json.dumps(payload).encode()
    monkeypatch.setattr(
        uptime_ping.urllib.request,
        "urlopen",
        lambda req, timeout=None: _FakeResponse(body, code),
    )
    return len(body)


def test_large_health_payload_is_not_truncated(monkeypatch):
    """Un payload bien por encima del viejo corte de 2048 B debe parsear."""
    size = _patch_urlopen(monkeypatch, _health_payload(extra_bytes=8192))
    assert size > 2048, "el payload de prueba debe superar el corte histórico"

    ok, detail = uptime_ping._probe("https://api.example.com")

    assert ok is True, f"prod sana no puede reportarse caída: {detail}"
    assert "status=ok" in detail


def test_small_healthy_payload_still_works(monkeypatch):
    _patch_urlopen(monkeypatch, _health_payload())
    ok, detail = uptime_ping._probe("https://api.example.com")
    assert ok is True and "status=ok" in detail


def test_read_cap_is_far_above_the_real_payload():
    """El tope es una guarda anti-respuesta-gigante, no un presupuesto ajustado.

    /health medía ~3,7 KB el 2026-07-25; el tope debe dejar margen de sobra
    para que el payload pueda seguir creciendo sin volver a truncar.
    """
    assert uptime_ping._MAX_BODY_BYTES >= 64 * 1024


def test_real_outage_is_still_detected(monkeypatch):
    """El arreglo no puede volver ciego al monitor: 503 sigue siendo caída."""
    _patch_urlopen(monkeypatch, {"status": "down"}, code=503)
    ok, detail = uptime_ping._probe("https://api.example.com")
    assert ok is False
    assert "503" in detail


def test_rolling_deploy_degraded_200_is_up(monkeypatch):
    """EL FIX 2026-07-28: durante un rolling deploy, /health/deploy devuelve
    200 con status=degraded (worker_fleet_incoherent). Eso NO debe paginar —
    si no, el ping rojo bloquea el propio deploy en Railway ("Wait for CI"),
    y la flota nunca converge (deadlock auto-reforzado)."""
    _patch_urlopen(monkeypatch, {"status": "degraded",
                                 "degraded_reason": "worker_fleet_incoherent"})
    ok, detail = uptime_ping._probe("https://api.example.com")
    assert ok is True, f"un rollout degradado no es una caída: {detail}"
    assert "degraded" in detail


def test_probe_targets_health_deploy_not_strict_health(monkeypatch):
    """Debe pegar a /health/deploy (rollout-safe), NO a /health estricto —
    /health 503ea en cada rollout y eso es lo que dispara el deadlock."""
    seen = {}

    def _fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        return _FakeResponse(json.dumps({"status": "ok"}).encode())

    monkeypatch.setattr(uptime_ping.urllib.request, "urlopen", _fake_urlopen)
    uptime_ping._probe("https://api.example.com")
    assert seen["url"].endswith("/health/deploy"), seen["url"]


def test_http_error_503_is_down(monkeypatch):
    """Un 503 real (urllib lo levanta como HTTPError) sigue siendo caída —
    el fix no puede volver ciego al monitor ante un outage de DB/Redis."""
    def _raise_503(req, timeout=None):
        raise uptime_ping.urllib.error.HTTPError(
            req.full_url, 503, "Service Unavailable", {},
            io.BytesIO(b'{"status":"down","down_reason":"db_down"}'),
        )

    monkeypatch.setattr(uptime_ping.urllib.request, "urlopen", _raise_503)
    ok, detail = uptime_ping._probe("https://api.example.com")
    assert ok is False
    assert "503" in detail


def test_genuinely_non_json_body_is_reported(monkeypatch):
    """Un cuerpo que de verdad no es JSON (ej. HTML de un proxy) sigue fallando."""
    monkeypatch.setattr(
        uptime_ping.urllib.request,
        "urlopen",
        lambda req, timeout=None: _FakeResponse(b"<html>502 Bad Gateway</html>"),
    )
    ok, detail = uptime_ping._probe("https://api.example.com")
    assert ok is False
    assert "non-JSON" in detail
