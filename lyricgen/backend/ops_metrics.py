"""Small Redis-backed counters for remediation signals and alert queries."""

import os
from datetime import datetime, timezone

_KEY = "genly:ops:metrics"


def _client():
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        return None
    from redis import Redis
    return Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1)


def increment(name: str, amount: int = 1) -> None:
    """Best-effort fleet counter. Observability must never break a request."""
    try:
        client = _client()
        if client is None:
            return
        field = f"{datetime.now(timezone.utc):%Y-%m-%d}:{name}"
        client.hincrby(_KEY, field, int(amount))
        client.expire(_KEY, 45 * 24 * 3600)
    except Exception:
        pass


def snapshot() -> dict[str, int]:
    try:
        client = _client()
        if client is None:
            return {}
        raw = client.hgetall(_KEY)
        return {
            (key.decode() if isinstance(key, bytes) else str(key)):
            int(value.decode() if isinstance(value, bytes) else value)
            for key, value in raw.items()
        }
    except Exception:
        return {}
