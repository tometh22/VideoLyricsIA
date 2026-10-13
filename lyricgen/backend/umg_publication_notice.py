"""Tell UMG, by mail, that a correction they asked for is now published.

OFF by default and with NO recipients: nothing is ever sent until an operator sets both
``UMG_PUBLISH_NOTIFY_ENABLED=1`` and the recipient list for the portal. Real recipients
are an explicit decision of the owner, never a code default.

* ``UMG_PUBLISH_NOTIFY_RECIPIENTS_ARGENTINA`` / ``..._CHILE`` (comma separated; falls back to
  ``UMG_PUBLISH_NOTIFY_RECIPIENTS``), at most ``MAX_RECIPIENTS`` valid addresses.
* Mail goes through ``emails._send_email``, whose staging gate drops or redirects any address
  that is not in ``EMAIL_STAGING_ALLOWLIST`` outside production. So staging (which writes to
  the live portal database) only reaches a real recipient if that address was allow-listed on
  purpose.
* Only a publication that ANSWERS client requests (content changed and at least one request
  closed by it) and whose files are visible to the client notifies; a plain re-send or a
  hidden delivery stays silent.
"""
from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger("uvicorn.error")

MAX_RECIPIENTS = 10
_TRUE = {"1", "true", "yes", "on"}
_EMAIL = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")
_PORTAL_URLS = {"argentina": "https://umg.genly.pro", "chile": "https://umgchile.genly.pro"}


def enabled() -> bool:
    return os.environ.get("UMG_PUBLISH_NOTIFY_ENABLED", "").strip().lower() in _TRUE


def recipients(portal_id: str | None) -> list[str]:
    portal = (portal_id or "argentina").strip().lower()
    raw = (os.environ.get(f"UMG_PUBLISH_NOTIFY_RECIPIENTS_{portal.upper()}")
           or os.environ.get("UMG_PUBLISH_NOTIFY_RECIPIENTS") or "")
    seen: list[str] = []
    for item in re.split(r"[,;\n]", raw):
        address = item.strip()
        if address and _EMAIL.match(address) and address.lower() not in {a.lower() for a in seen}:
            seen.append(address)
    return seen[:MAX_RECIPIENTS]


def portal_url(portal_id: str | None) -> str:
    portal = (portal_id or "argentina").strip().lower()
    return (os.environ.get(f"UMG_PORTAL_URL_{portal.upper()}") or _PORTAL_URLS.get(portal) or _PORTAL_URLS["argentina"]).rstrip("/")


def should_notify(*, content_changed: bool, resolved_requests, hidden_from_client: bool) -> bool:
    """A correction that closed client requests and is visible to the client."""
    return bool(enabled() and content_changed and resolved_requests and not hidden_from_client)


def notify(*, artist: str, song: str, portal_id: str | None, revision: int, comments: list[str]) -> int:
    """Send the notice to every recipient of the portal. Never raises; returns how many were handed to the mailer."""
    addresses = recipients(portal_id)
    if not addresses:
        logger.info("[UMG-NOTICE] activado pero sin destinatarios para %s: no se envía nada", portal_id)
        return 0
    import emails
    sent = 0
    for address in addresses:
        try:
            emails.send_umg_publication_notice(
                address, artist=artist, song=song, portal_id=portal_id, revision=revision,
                comments=comments, portal_url=portal_url(portal_id),
            )
            sent += 1
        except Exception:
            logger.warning("[UMG-NOTICE] fallo el envío a un destinatario", exc_info=True)
    logger.info("[UMG-NOTICE] portal=%s versión=%s enviados=%d/%d", portal_id, revision, sent, len(addresses))
    return sent
