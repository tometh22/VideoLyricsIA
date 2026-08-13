"""Tiny Redis-backed response cache for hot GET endpoints (2026-05-30).

Rationale: the operator's perceived latency for "snappy" endpoints
(/usage, eventually /auth/me) is dominated by the DB roundtrip + 150 ms
network from LATAM to Railway US-East. For values that are essentially
read-only over a 30-60 s window (the plan usage counter only moves
when the operator approves or rejects), caching at the edge of the
endpoint amortises the DB hit across every reader.

Contract:

* `get_or_set_json(key, ttl_s, compute)` returns the cached value if
  Redis has one, otherwise calls `compute()`, persists the result, and
  returns it.
* `invalidate(key)` drops a cache entry. Callers MUST call this from
  mutation endpoints (approve/reject) so the next /usage read returns
  the live counter — otherwise the operator sees a stale "X / 250"
  for up to TTL seconds after their action, which is enough to
  confuse them in fast review sessions.

What this is NOT:

* It is NOT a write-through cache; mutations bypass the cache entirely
  and the read path repopulates.
* It is NOT a request coalescer; concurrent misses each hit the DB.
  That's fine: with a 30 s TTL the cache fills in after the first read
  and concurrent misses only happen at the TTL expiry boundary.
* It is NOT used by /jobs or /status — those return per-job state
  that changes more frequently than the TTL would tolerate.

Defaults to no-op when Redis isn't reachable so dev sessions and any
deploy with REDIS_URL unset just bypass the cache silently.
"""
from __future__ import annotations

import json
import logging
from typing import Callable, Any

logger = logging.getLogger("genly.cache")


def _redis():
    """Borrow the Redis connection that queue_jobs already lazy-inits.

    Sharing the connection (rather than opening a second one for cache
    reads) keeps the connection pool tight on Railway's small Redis tier.
    Returns None if Redis isn't configured or unreachable — every caller
    short-circuits to compute() in that case.
    """
    try:
        from queue_jobs import _init_redis
        r, _, _ = _init_redis()
        return r
    except Exception:  # pragma: no cover — defensive only
        return None


def get_or_set_json(
    key: str, ttl_s: int, compute: Callable[[], Any]
) -> Any:
    """Return the cached value if present, otherwise compute + cache.

    Failures on the Redis side are swallowed: the endpoint MUST stay
    correct even if Redis goes down. A degraded cache (slow miss) is
    much better than a 500 error.
    """
    r = _redis()
    if r is None:
        return compute()
    try:
        cached = r.get(key)
        if cached is not None:
            return json.loads(cached)
    except Exception as exc:  # pragma: no cover — Redis blip
        logger.warning("[cache] read failed for %s: %s", key, exc)
    # Miss (or read error): compute, then write through.
    result = compute()
    try:
        # SETEX is the atomic "value + TTL" path so we never leave a
        # forever-valid cache entry behind.
        r.setex(key, ttl_s, json.dumps(result, default=str))
    except Exception as exc:  # pragma: no cover
        logger.warning("[cache] write failed for %s: %s", key, exc)
    return result


def invalidate(key: str) -> None:
    """Drop a single cache key. Used by mutation endpoints after writes."""
    r = _redis()
    if r is None:
        return
    try:
        r.delete(key)
    except Exception as exc:  # pragma: no cover
        logger.warning("[cache] invalidate failed for %s: %s", key, exc)


# ─── Naming helpers ────────────────────────────────────────────────────
# Keep cache keys in one place so callers don't drift between read and
# invalidate paths. Format: "<endpoint>:<tenant>:<user>" so the keyspace
# stays browsable in redis-cli and unrelated tenants never collide.

def usage_key(tenant_id: str, user_id: int) -> str:
    return f"usage:{tenant_id}:{user_id}"
