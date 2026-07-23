"""Logs de Railway para el contexto de investigación y el comando /logs.

Requiere RAILWAY_PROJECT_TOKEN (Project Settings → Tokens, env production).
Sin token, todo degrada a "(logs de Railway no configurados)" — el Sentinel
funciona igual, solo que ciego a los logs.
"""

import logging
import os

import httpx

from logging_utils import redact

logger = logging.getLogger("sentinel.railway")

_GQL = "https://backboard.railway.com/graphql/v2"
# Dos formas de auth (Railway bloquea la creación de project tokens por API,
# así que soportamos ambas):
#  - RAILWAY_PROJECT_TOKEN → header Project-Access-Token (scoped al proyecto; el
#    más seguro, se crea a mano en Project Settings → Tokens).
#  - RAILWAY_API_TOKEN → header Authorization: Bearer (token de cuenta/personal;
#    funciona ya, pero alcanza TODOS los proyectos — reemplazar por el scoped
#    cuando se pueda).
_PROJECT_TOKEN = os.environ.get("RAILWAY_PROJECT_TOKEN", "")
_API_TOKEN = os.environ.get("RAILWAY_API_TOKEN", "")
_PROJECT = os.environ.get("RAILWAY_PROJECT_ID", "")
_ENV = os.environ.get("RAILWAY_ENVIRONMENT_ID", "")

DISABLED_MSG = ("(logs de Railway no configurados — falta RAILWAY_PROJECT_TOKEN "
                "o RAILWAY_API_TOKEN + RAILWAY_PROJECT_ID/ENVIRONMENT_ID)")


def _auth_headers() -> dict:
    if _PROJECT_TOKEN:
        return {"Project-Access-Token": _PROJECT_TOKEN}
    if _API_TOKEN:
        return {"Authorization": f"Bearer {_API_TOKEN}"}
    return {}


def enabled() -> bool:
    return bool((_PROJECT_TOKEN or _API_TOKEN) and _PROJECT and _ENV)


async def _gql(query: str, variables: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(_GQL, json={"query": query, "variables": variables or {}},
                         headers=_auth_headers())
    return r.json()


async def _latest_deployment_id(service_name: str) -> str | None:
    q = """query($p:String!){project(id:$p){services{edges{node{name
            serviceInstances{edges{node{environmentId latestDeployment{id}}}}}}}}}"""
    d = await _gql(q, {"p": _PROJECT})
    for e in (d.get("data", {}).get("project", {}).get("services", {}).get("edges") or []):
        n = e["node"]
        if n["name"].lower() != service_name.lower():
            continue
        for si in n["serviceInstances"]["edges"]:
            node = si["node"]
            if node["environmentId"] == _ENV and node.get("latestDeployment"):
                return node["latestDeployment"]["id"]
    return None


async def tail(service_name: str, lines: int = 80) -> str:
    """Últimas N líneas de logs del deployment activo del servicio."""
    if not enabled():
        return DISABLED_MSG
    try:
        dep = await _latest_deployment_id(service_name)
        if not dep:
            return f"(servicio {service_name!r} sin deployment activo en este env)"
        q = """query($d:String!,$l:Int!){deploymentLogs(deploymentId:$d,limit:$l){
                timestamp message severity}}"""
        d = await _gql(q, {"d": dep, "l": lines})
        logs = d.get("data", {}).get("deploymentLogs") or []
        if not logs:
            return f"(sin logs para {service_name})"
        return redact("\n".join(
            f"{(x.get('timestamp') or '')[:19]} {x.get('message','')}" for x in logs
        ))[-6000:]
    except Exception as e:  # nunca tumbar una investigación por logs
        logger.warning("railway logs falló: %s", redact(e))
        return redact(f"(error leyendo logs de Railway: {e})")


async def context_for_investigation() -> str:
    """Cola de logs de Worker + api para adjuntar a la investigación."""
    if not enabled():
        return DISABLED_MSG
    parts = []
    for svc in ("Worker", "api"):
        parts.append(f"===== logs recientes: {svc} =====\n{await tail(svc, 60)}")
    return redact("\n\n".join(parts))
