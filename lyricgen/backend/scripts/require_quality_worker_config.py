#!/usr/bin/env python3
"""Fail closed when the isolated quality worker cannot execute real jobs.

The resource gate proves that the container is bounded.  This companion gate
proves that it can reach every durable dependency used by ``quality_jobs``.
Presence and bounded connectivity are checked; secrets are never printed.
"""
from __future__ import annotations

import os
import re
from collections.abc import Callable
from urllib.request import Request, urlopen


MIN_HMAC_SECRET_BYTES = 32
MIN_HMAC_SECRET_DISTINCT_BYTES = 12
_HMAC_KEY_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}\Z")


def _strong_hmac_secret(value: object) -> bool:
    """Reject short and obvious placeholder keys without exposing entropy."""
    if not isinstance(value, str):
        return False
    encoded = value.encode("utf-8")
    return (
        len(encoded) >= MIN_HMAC_SECRET_BYTES
        and len(set(encoded)) >= MIN_HMAC_SECRET_DISTINCT_BYTES
    )


def _valid_hmac_key_id(value: object) -> bool:
    return isinstance(value, str) and bool(_HMAC_KEY_ID_RE.fullmatch(value.strip()))


def _present(env: dict[str, str], *names: str) -> bool:
    return any(str(env.get(name, "")).strip() for name in names)


def validate_config(env: dict[str, str]) -> list[str]:
    errors: list[str] = []
    requirements = {
        "database": ("DATABASE_URL",),
        "redis": ("REDIS_URL",),
        "quality_cache": ("QUALITY_CACHE_REDIS_URL",),
        "object_access_key": ("R2_ACCESS_KEY_ID", "S3_ACCESS_KEY", "S3_ACCESS_KEY_ID"),
        "object_secret_key": (
            "R2_SECRET_ACCESS_KEY", "S3_SECRET_KEY", "S3_SECRET_ACCESS_KEY",
        ),
        "object_endpoint": ("R2_ENDPOINT_URL", "S3_ENDPOINT_URL"),
        "object_bucket": ("R2_BUCKET", "S3_BUCKET"),
        "content_attestation_key": (
            "QUALITY_CONTENT_FINGERPRINT_HMAC_KEY",
            "QUALITY_CONTENT_ATTESTATION_KEY",
            "QUALITY_LEARNING_HMAC_KEY",
        ),
    }
    for label, names in requirements.items():
        if not _present(env, *names):
            errors.append(f"missing_{label}")

    hmac_key = next((
        str(env.get(name) or "")
        for name in (
            "QUALITY_CONTENT_FINGERPRINT_HMAC_KEY",
            "QUALITY_CONTENT_ATTESTATION_KEY",
            "QUALITY_LEARNING_HMAC_KEY",
        )
        if str(env.get(name) or "").strip()
    ), "")
    if hmac_key and not _strong_hmac_secret(hmac_key):
        errors.append("weak_content_attestation_key")
    hmac_key_id = (
        env.get("QUALITY_CONTENT_FINGERPRINT_HMAC_KEY_ID")
        or env.get("QUALITY_LEARNING_HMAC_KEY_ID")
    )
    if hmac_key and not _valid_hmac_key_id(hmac_key_id):
        errors.append("missing_or_invalid_hmac_key_id")

    if str(env.get("QUEUES", "")).strip() != "transcription_quality":
        errors.append("queue_not_isolated")
    if str(env.get("TRANSCRIPTION_QUALITY_QUEUE_ENABLED", "")).strip().lower() not in {
        "1", "true", "yes", "on",
    }:
        errors.append("quality_queue_disabled")
    if str(env.get("VOCAL_SEP_ENABLED", "")).strip().lower() not in {
        "1", "true", "yes", "on",
    }:
        errors.append("vocal_separation_disabled")
    if not _present(env, "REPLICATE_API_TOKEN"):
        errors.append("missing_vocal_separator_provider")
    if str(env.get("TARGETED_CONSENSUS_ENABLED", "0")).strip().lower() in {
        "1", "true", "yes", "on",
    } and not _present(env, "OPENAI_API_KEY"):
        errors.append("missing_targeted_asr_provider")
    return errors


def _database_probe() -> bool:
    from sqlalchemy import text
    from database import engine
    with engine.connect() as connection:
        return connection.execute(text("SELECT 1")).scalar() == 1


def _redis_probe(url: str) -> bool:
    import redis
    client = redis.Redis.from_url(
        url, socket_connect_timeout=3, socket_timeout=3,
        health_check_interval=0,
    )
    try:
        return bool(client.ping())
    finally:
        client.close()


def _r2_probe() -> bool:
    from storage import probe_r2
    ok, _elapsed_ms, _error = probe_r2()
    return bool(ok)


def _provider_get(url: str, authorization: str) -> bool:
    request = Request(url, headers={
        "Authorization": authorization,
        "User-Agent": "genly-quality-preflight/1",
    })
    with urlopen(request, timeout=5) as response:
        return 200 <= int(response.status) < 300


def _replicate_probe(token: str) -> bool:
    return _provider_get("https://api.replicate.com/v1/account", f"Token {token}")


def _openai_probe(token: str) -> bool:
    return _provider_get("https://api.openai.com/v1/models", f"Bearer {token}")


def connectivity_errors(
    env: dict[str, str], *,
    database_probe: Callable[[], bool] = _database_probe,
    redis_probe: Callable[[str], bool] = _redis_probe,
    r2_probe: Callable[[], bool] = _r2_probe,
    replicate_probe: Callable[[str], bool] = _replicate_probe,
    openai_probe: Callable[[str], bool] = _openai_probe,
) -> list[str]:
    """Check durable dependencies with redacted, stable error codes."""
    checks = (
        ("database_unreachable", lambda: database_probe()),
        ("redis_unreachable", lambda: redis_probe(env["REDIS_URL"])),
        (
            "quality_cache_unreachable",
            lambda: redis_probe(env["QUALITY_CACHE_REDIS_URL"]),
        ),
        ("object_storage_unreachable", lambda: r2_probe()),
    )
    errors: list[str] = []
    for code, probe in checks:
        try:
            if not probe():
                errors.append(code)
        except Exception:
            errors.append(code)
    replicate_token = str(env.get("REPLICATE_API_TOKEN") or "").strip()
    if replicate_token:
        try:
            if not replicate_probe(replicate_token):
                errors.append("vocal_separator_provider_unreachable")
        except Exception:
            errors.append("vocal_separator_provider_unreachable")
    targeted = str(env.get("TARGETED_CONSENSUS_ENABLED", "0")).strip().lower() in {
        "1", "true", "yes", "on",
    }
    openai_token = str(env.get("OPENAI_API_KEY") or "").strip()
    if targeted and openai_token:
        try:
            if not openai_probe(openai_token):
                errors.append("targeted_asr_provider_unreachable")
        except Exception:
            errors.append("targeted_asr_provider_unreachable")
    return errors


def main() -> int:
    env = dict(os.environ)
    errors = validate_config(env)
    if not errors:
        errors.extend(connectivity_errors(env))
    if errors:
        print("[QUALITY-WORKER][CONFIG-GATE] " + ",".join(errors))
        return 1
    print("[QUALITY-WORKER][CONFIG-GATE] dependencies configured; secrets redacted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
