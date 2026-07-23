"""Runtime operational controls shared by API replicas.

The submissions switch lives in Redis so an operator can stop new expensive
work without a Railway redeploy. An environment flag remains the bootstrap
fallback for maintenance/recovery when Redis is unavailable.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

_KEY = "ops:submissions_paused"
_LAST_VALID_STATE: dict | None = None


def _env_paused() -> bool:
    return os.environ.get("SUBMISSIONS_PAUSED", "0").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _client():
    redis_url = os.environ.get("REDIS_URL", "").strip()
    if not redis_url:
        return None
    from redis import Redis
    return Redis.from_url(
        redis_url,
        socket_connect_timeout=2,
        socket_timeout=2,
        decode_responses=True,
    )


def get_submissions_state() -> dict:
    global _LAST_VALID_STATE
    fallback = {
        "paused": _env_paused(),
        "reason": "environment maintenance switch" if _env_paused() else "",
        "until": None,
        "retry_after": 60,
        "source": "environment",
    }
    client = _client()
    if client is None:
        return fallback
    try:
        raw = client.get(_KEY)
    except Exception:
        return _control_failure_state(fallback, "redis_unavailable")
    if not raw:
        if _LAST_VALID_STATE is not None:
            return {**_LAST_VALID_STATE, "source": "cached_missing_key"}
        return fallback
    try:
        state = json.loads(raw)
    except (TypeError, ValueError):
        return _control_failure_state(fallback, "invalid_json")
    until = state.get("until")
    if until:
        try:
            deadline = datetime.fromisoformat(str(until).replace("Z", "+00:00"))
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            if deadline <= datetime.now(timezone.utc):
                return {**fallback, "source": "expired"}
        except ValueError:
            return _control_failure_state(fallback, "invalid_until")
    normalized = {
        "paused": bool(state.get("paused")),
        "reason": str(state.get("reason") or ""),
        "until": until,
        "retry_after": max(1, min(int(state.get("retry_after") or 60), 3600)),
        "source": "redis",
    }
    _LAST_VALID_STATE = normalized
    return normalized


def _control_failure_state(fallback: dict, reason: str) -> dict:
    """Never lose an active pause because the control plane is unhealthy."""
    if _LAST_VALID_STATE is not None and _LAST_VALID_STATE.get("paused"):
        return {
            **_LAST_VALID_STATE,
            "source": "cached_fail_closed",
            "control_error": reason,
        }
    env = os.environ.get("ENVIRONMENT", os.environ.get("APP_ENV", "development")).lower()
    if env in {"prod", "production"}:
        return {
            "paused": True,
            "reason": "submissions control unavailable",
            "until": None,
            "retry_after": 60,
            "source": "fail_closed",
            "control_error": reason,
        }
    return {**fallback, "control_error": reason}


def set_submissions_state(*, paused: bool, reason: str = "", until=None,
                          retry_after: int = 60) -> dict:
    global _LAST_VALID_STATE
    client = _client()
    if client is None:
        raise RuntimeError("REDIS_URL is required for dynamic operations controls")
    state = {
        "paused": bool(paused),
        "reason": str(reason or "")[:500],
        "until": until.isoformat() if hasattr(until, "isoformat") else until,
        "retry_after": max(1, min(int(retry_after or 60), 3600)),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    client.set(_KEY, json.dumps(state, separators=(",", ":")))
    normalized = {**state, "source": "redis"}
    _LAST_VALID_STATE = normalized
    return normalized


# Every path here creates a job, initiates an upload, or enqueues expensive
# work. Multipart part/complete/abort are intentionally absent: already-started
# uploads must be allowed to finish or clean themselves up.
_BLOCKED_EXACT = {
    "/upload-url",
    "/upload-multipart-init",
    "/upload",
    "/transcribe",
    "/transcribe-uploaded",
    "/generate",
    "/generate-preview",
}
_BLOCKED_PREFIXES = (
    "/edit/",
    "/retry/",
    "/enable-prores/",
    "/jobs/",  # narrowed below to producer suffixes
    "/scenes/",
    "/background-preview",
    "/background-preview/",
    "/admin/prewarm",
    "/youtube/upload/",
)
_JOB_PRODUCER_SUFFIXES = (
    "/edit", "/retry", "/variants", "/variant", "/reanchor",
    "/scene-regenerate", "/background-preview", "/deliver-to-drive",
)


def is_submission_path(method: str, path: str) -> bool:
    if method.upper() not in {"POST", "PUT", "PATCH"}:
        return False
    normalized = path.rstrip("/") or "/"
    if normalized in _BLOCKED_EXACT:
        return True
    if normalized.startswith("/jobs/"):
        return (
            normalized.endswith(_JOB_PRODUCER_SUFFIXES)
            or ("/scenes/" in normalized and normalized.endswith("/regenerate"))
        )
    return normalized.startswith(tuple(p.rstrip("/") + "/" for p in _BLOCKED_PREFIXES if p != "/jobs/"))
