"""Parseo + verificación del webhook de Sentry.

Sentry (Internal Integration) firma cada request con
`sentry-hook-signature: HMAC_SHA256(body, client_secret)`. Sin secret
configurado NO verificamos (solo para dev local) — en prod configurarlo
siempre, si no cualquiera que descubra la URL puede disparar corridas
del agente (= gastar tokens) con payloads inventados.

Soportamos los dos shapes que manda Sentry:
  - "event_alert" / "metric_alert": data.event  (issue_id en data.event.issue_id)
  - "issue" (issue created/escalating):        data.issue
"""

import hashlib
import hmac
import json

from logging_utils import redact


def verify_signature(body: bytes, signature: str | None, secret: str) -> bool:
    if not secret:
        return True  # dev only — ver docstring
    if not signature:
        return False
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, signature)


def parse_alert(payload: dict) -> dict | None:
    """Normaliza el payload a {issue_id, title, culprit, level, url}.

    Devuelve None si el payload no trae un issue reconocible (p. ej. pings
    de instalación de la integración).
    """
    data = payload.get("data") or {}

    issue = data.get("issue")
    if issue:
        return {
            "issue_id": str(issue.get("id", "")),
            "title": issue.get("title") or "(sin título)",
            "culprit": issue.get("culprit") or "",
            "level": issue.get("level") or "",
            "url": (issue.get("web_url") or issue.get("url") or ""),
        }

    event = data.get("event")
    if event:
        return {
            "issue_id": str(event.get("issue_id") or event.get("issue.id") or ""),
            "title": event.get("title") or event.get("message") or "(sin título)",
            "culprit": event.get("culprit") or "",
            "level": event.get("level") or "",
            "url": event.get("web_url") or event.get("issue_url") or "",
        }

    return None


def compact_context(payload: dict, max_chars: int = 6000) -> str:
    """El payload crudo (recortado) que le pasamos al agente como contexto.
    Sentry manda stacktraces/tags adentro — es exactamente lo que el agente
    necesita para arrancar sin ir a buscar nada."""
    text = json.dumps(payload, ensure_ascii=False, default=str)
    return redact(text)[:max_chars]
