"""Tests for the Redis-backed response cache (2026-05-30 perf).

The helper is small but covers the critical path of /usage. Pin:
  - cache HIT returns the persisted value
  - cache MISS computes + persists + returns
  - invalidate() drops a key so the next read recomputes
  - When Redis is unavailable, every call passes through to compute()
    transparently (no exception, no log spam) so a degraded Redis tier
    never takes /usage with it

Uses a fake Redis to keep the tests hermetic — no live Redis dependency.
"""
from __future__ import annotations

import os
import sys

import pytest

# Bring backend modules into path before any backend import.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class FakeRedis:
    """Minimal in-memory Redis stand-in covering the methods cache.py
    uses (get, setex, delete). TTL is honoured for the assertions we
    care about (we don't need real expiry timing in unit tests)."""

    def __init__(self):
        self.store: dict[str, bytes] = {}

    def get(self, key):
        v = self.store.get(key)
        return v.encode() if isinstance(v, str) else v

    def setex(self, key, ttl_s, value):
        # Ignore TTL — tests verify the SET happens, not the expiry.
        self.store[key] = value

    def delete(self, key):
        self.store.pop(key, None)


@pytest.fixture
def fake_redis(monkeypatch):
    """Patch cache._redis to return a fresh FakeRedis per test."""
    import cache
    fake = FakeRedis()
    monkeypatch.setattr(cache, "_redis", lambda: fake)
    return fake


def test_cache_miss_runs_compute_and_persists(fake_redis):
    """First call hits the compute path; second hits the cache."""
    import cache
    calls = []
    def compute():
        calls.append("ran")
        return {"used": 25, "limit": 250}

    out1 = cache.get_or_set_json("usage:t1:5", ttl_s=30, compute=compute)
    out2 = cache.get_or_set_json("usage:t1:5", ttl_s=30, compute=compute)

    assert out1 == {"used": 25, "limit": 250}
    assert out2 == {"used": 25, "limit": 250}
    # compute() only ran on the miss, not on the hit.
    assert calls == ["ran"]


def test_invalidate_drops_the_key(fake_redis):
    """Invalidate makes the next read fall back to compute again."""
    import cache
    calls = []
    def compute():
        calls.append("ran")
        return {"used": 25}

    cache.get_or_set_json("usage:t1:5", ttl_s=30, compute=compute)
    assert calls == ["ran"]

    cache.invalidate("usage:t1:5")
    cache.get_or_set_json("usage:t1:5", ttl_s=30, compute=compute)
    # Compute ran TWICE now — the cache was empty on the second call.
    assert calls == ["ran", "ran"]


def test_keys_are_scoped_by_tenant_and_user(fake_redis):
    """Two operators on the same tenant must not see each other's cache."""
    import cache
    cache.get_or_set_json("usage:omg:1", ttl_s=30, compute=lambda: {"u": 1})
    cache.get_or_set_json("usage:omg:2", ttl_s=30, compute=lambda: {"u": 2})

    assert cache.get_or_set_json("usage:omg:1", ttl_s=30,
                                 compute=lambda: {"never": True}) == {"u": 1}
    assert cache.get_or_set_json("usage:omg:2", ttl_s=30,
                                 compute=lambda: {"never": True}) == {"u": 2}


def test_no_redis_passes_through_to_compute(monkeypatch):
    """When Redis isn't reachable, every call runs compute() directly
    without raising. The endpoint stays correct, just slower."""
    import cache
    monkeypatch.setattr(cache, "_redis", lambda: None)

    calls = []
    def compute():
        calls.append("ran")
        return {"used": 25}

    # Two calls in a row both go through compute() because there's no
    # cache to serve them.
    cache.get_or_set_json("usage:nope", ttl_s=30, compute=compute)
    cache.get_or_set_json("usage:nope", ttl_s=30, compute=compute)
    assert calls == ["ran", "ran"]

    # Invalidate is a no-op (doesn't raise).
    cache.invalidate("usage:nope")


def test_usage_key_format():
    """The key namespace is browsable in redis-cli and never collides
    across tenants — pin the exact format the endpoint relies on."""
    from cache import usage_key
    assert usage_key("umg_archive", 12) == "usage:umg_archive:12"
    assert usage_key("default", 1) == "usage:default:1"
