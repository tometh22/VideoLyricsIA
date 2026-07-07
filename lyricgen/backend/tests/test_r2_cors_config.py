"""Guard contra drift de dominios en la política CORS de R2.

Incidente agus77 (06/07): el bucket rechazaba con 403 el preflight desde
https://staging.genly.pro — el dominio REAL del frontend de staging
(FRONTEND_URL) — porque r2_cors.json listaba staging.APP.genly.pro, un
dominio que no existe/no se usa. Resultado: NINGÚN upload directo a R2
(single-PUT ni multipart) funcionaba desde el navegador en staging; el
wizard moría con el banner genérico 'Sin respuesta del servidor'.

Este test fija los dominios canónicos que la política DEBE incluir. Si
alguien cambia el dominio del frontend, tiene que actualizar r2_cors.json
(y correr scripts/configure_r2_cors.sh — los browsers cachean el preflight
hasta 50 min).
"""
import json
from pathlib import Path

_POLICY = Path(__file__).resolve().parent.parent / "scripts" / "r2_cors.json"

# Dominios que sirven el frontend hoy (Railway FRONTEND_URL por ambiente).
_CANONICAL = {
    "https://app.genly.pro",        # producción
    "https://staging.genly.pro",    # staging (incidente 06/07)
}


def test_cors_policy_includes_canonical_frontends():
    rules = json.loads(_POLICY.read_text())["CORSRules"]
    origins = {o for r in rules for o in r.get("AllowedOrigins", [])}
    missing = _CANONICAL - origins
    assert not missing, (
        f"r2_cors.json no permite {missing} — los uploads directos a R2 "
        f"fallarán con 403 de preflight desde ese frontend (y el wizard "
        f"muestra 'Sin respuesta del servidor'). Agregalo y aplicá "
        f"scripts/configure_r2_cors.sh.")


def test_cors_policy_exposes_etag():
    """Sin ExposeHeaders: ETag el multipart sube 100% y muere en el
    complete (el JS lee etag=null) — fallo carísimo de diagnosticar."""
    rules = json.loads(_POLICY.read_text())["CORSRules"]
    assert any("ETag" in (r.get("ExposeHeaders") or []) for r in rules)
