"""Veo 429 circuit breaker — cross-worker, Redis-backed, fail-CLOSED.

During a project-wide Veo quota event, every worker would otherwise burn the
full 5×backoff (~5.5 min in-slot) before falling to gradient — a cascade that
holds render slots AND sustains 429 pressure so quota never recovers. This
breaker lets the first few 429s (across workers) trip a shared flag; subsequent
jobs skip Veo and fall straight to gradient until a half-open probe finds Veo
healthy again.

Design (folds in the Tier-3b adversarial review):
- TRIP on a cross-worker rolling counter (VEO_BREAKER_TRIP_THRESHOLD in a
  VEO_BREAKER_WINDOW_S window), NOT on one job reaching MAX_ATTEMPTS — so the
  first wave is protected, and a lone transient 429 never trips it (hysteresis).
- HALF-OPEN recovery: while open, exactly ONE worker per VEO_BREAKER_PROBE_S
  window is let through (SET NX probe lock) to test Veo; its success closes the
  breaker for everyone. Avoids the thundering-herd sawtooth of a pure fixed-TTL.
- FAIL-CLOSED: breaker disabled OR any Redis error → is_open() returns False →
  the caller tries Veo (current behavior). A Redis blip must NEVER force every
  job to gradient — that would degrade quality when Redis, not Veo, is the
  problem (Tier 1 concern).
- ENV-GATED OFF by default (VEO_CIRCUIT_BREAKER_ENABLED=0). Flip on in staging
  after soak; matches the house "default = current behavior" rule.
- Tenant-keyable (pass tenant=...) so a tenant with its own Vertex quota can get
  an independent breaker later; default key is global (quota is project-wide).
"""

import logging
import os

logger = logging.getLogger("genly.veo_breaker")


def _enabled() -> bool:
    # Read at call time so tests/operators can toggle without reimport.
    return os.environ.get("VEO_CIRCUIT_BREAKER_ENABLED", "0").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _cfg() -> dict:
    return {
        "ttl": int(os.environ.get("VEO_BREAKER_TTL_S", "120")),
        "threshold": int(os.environ.get("VEO_BREAKER_TRIP_THRESHOLD", "3")),
        "window": int(os.environ.get("VEO_BREAKER_WINDOW_S", "60")),
        "probe": int(os.environ.get("VEO_BREAKER_PROBE_S", "30")),
    }


def _redis():
    """Shared Redis handle (None if unavailable). Patchable in tests."""
    try:
        from queue_jobs import _init_redis
        r, _, _ = _init_redis()
        return r
    except Exception:
        return None


def _keys(tenant=None):
    suffix = f":{tenant}" if tenant else ""
    return (f"veo:rl:open{suffix}", f"veo:rl:count{suffix}", f"veo:rl:probe{suffix}")


def record_rate_limit(tenant=None) -> None:
    """Call on a Veo 429. Increments a windowed cross-worker counter; OPENS the
    breaker once the count crosses the threshold. Fail-soft (never raises)."""
    if not _enabled():
        return
    r = _redis()
    if r is None:
        return
    open_k, count_k, probe_k = _keys(tenant)
    cfg = _cfg()
    try:
        n = r.incr(count_k)
        if n == 1:
            r.expire(count_k, cfg["window"])
        if n >= cfg["threshold"]:
            r.setex(open_k, cfg["ttl"], "1")
            r.delete(probe_k)  # fresh probe allowed under the new open window
            logger.warning(
                "[VEO-BREAKER] OPEN (429 count=%s >= %s) — jobs fall to gradient for %ss",
                n, cfg["threshold"], cfg["ttl"],
            )
    except Exception:
        pass  # fail-soft: never block Veo on breaker bookkeeping


def record_success(tenant=None) -> None:
    """Call on a successful Veo submit. CLOSES the breaker (a probe succeeded =
    Veo is healthy again) and resets the window. Fail-soft."""
    if not _enabled():
        return
    r = _redis()
    if r is None:
        return
    open_k, count_k, probe_k = _keys(tenant)
    try:
        if r.delete(open_k):
            logger.info("[VEO-BREAKER] CLOSED — Veo healthy again")
        r.delete(count_k)
        r.delete(probe_k)
    except Exception:
        pass


def is_open(tenant=None) -> bool:
    """True → skip Veo and fall straight to gradient.

    FAIL-CLOSED: disabled or any Redis error → False (try Veo = current
    behavior). HALF-OPEN: while open, exactly one worker per probe window is let
    through (returns False) to test Veo — its record_success() closes the
    breaker for everyone, so there's no thundering herd on recovery.
    """
    if not _enabled():
        return False
    r = _redis()
    if r is None:
        return False  # fail-closed
    open_k, _count_k, probe_k = _keys(tenant)
    cfg = _cfg()
    try:
        if not r.get(open_k):
            return False  # breaker closed → try Veo
        # Breaker is open. Let exactly one worker probe per window.
        if r.set(probe_k, "1", nx=True, ex=cfg["probe"]):
            logger.info("[VEO-BREAKER] half-open probe — one job tries Veo")
            return False  # we're the prober → try Veo (success closes breaker)
        return True  # stay open → gradient fallback
    except Exception:
        return False  # fail-closed


def state(tenant=None) -> dict:
    """Best-effort breaker state for /health and operators. Never raises."""
    if not _enabled():
        return {"enabled": False, "open": False}
    r = _redis()
    if r is None:
        return {"enabled": True, "open": False, "redis": "down"}
    open_k, count_k, _ = _keys(tenant)
    try:
        is_o = bool(r.get(open_k))
        ttl = r.ttl(open_k) if is_o else 0
        return {"enabled": True, "open": is_o, "ttl_s": ttl,
                "recent_429": int(r.get(count_k) or 0)}
    except Exception:
        return {"enabled": True, "open": False, "redis": "error"}
