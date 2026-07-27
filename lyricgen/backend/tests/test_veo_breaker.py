"""Tier 3b — Veo circuit breaker state machine.

The breaker's complex logic (rolling trip, half-open probe, fail-closed) is
isolated in veo_breaker.py and fully unit-tested here against a fake Redis, so
the pipeline.py hooks stay thin. Folds in the adversarial review's required
behaviors: trip on a cross-worker counter (not single-job MAX), half-open probe
(no thundering herd), fail-closed on any Redis error / disabled.
"""

import pytest

import veo_breaker


class FakeRedis:
    """Minimal in-memory Redis for the breaker's surface (get/set NX EX, incr,
    expire, setex, delete, ttl). No real TTL expiry — tests drive state."""
    def __init__(self):
        self.kv = {}

    def get(self, k):
        return self.kv.get(k)

    def set(self, k, v, nx=False, ex=None):
        if nx and k in self.kv:
            return None
        self.kv[k] = v
        return True

    def setex(self, k, ttl, v):
        self.kv[k] = v
        return True

    def incr(self, k):
        self.kv[k] = int(self.kv.get(k, 0)) + 1
        return self.kv[k]

    def expire(self, k, ttl):
        return True

    def delete(self, *ks):
        n = 0
        for k in ks:
            if k in self.kv:
                del self.kv[k]
                n += 1
        return n

    def ttl(self, k):
        return 100 if k in self.kv else -2


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setenv("VEO_CIRCUIT_BREAKER_ENABLED", "1")
    monkeypatch.setenv("VEO_BREAKER_TRIP_THRESHOLD", "3")
    r = FakeRedis()
    monkeypatch.setattr(veo_breaker, "_redis", lambda: r)
    return r


# --- fail-closed (the safety-critical default) ---
def test_disabled_is_never_open(monkeypatch):
    monkeypatch.setenv("VEO_CIRCUIT_BREAKER_ENABLED", "0")
    assert veo_breaker.is_open() is False
    # record_* are no-ops when disabled (don't even touch redis)
    veo_breaker.record_rate_limit()


def test_redis_down_fails_closed(monkeypatch):
    monkeypatch.setenv("VEO_CIRCUIT_BREAKER_ENABLED", "1")
    monkeypatch.setattr(veo_breaker, "_redis", lambda: None)
    assert veo_breaker.is_open() is False  # try Veo, never force gradient


def test_redis_error_fails_closed(monkeypatch):
    monkeypatch.setenv("VEO_CIRCUIT_BREAKER_ENABLED", "1")

    class Boom:
        def get(self, *a, **k):
            raise RuntimeError("redis blip")
    monkeypatch.setattr(veo_breaker, "_redis", lambda: Boom())
    assert veo_breaker.is_open() is False


# --- trip threshold (hysteresis) ---
def test_does_not_trip_below_threshold(fake):
    veo_breaker.record_rate_limit()
    veo_breaker.record_rate_limit()  # 2 < threshold 3
    assert veo_breaker.is_open() is False


def test_trips_at_threshold(fake):
    for _ in range(3):
        veo_breaker.record_rate_limit()
    # Now open: the FIRST is_open() wins the probe (half-open) → False, then
    # subsequent callers stay open → True.
    assert veo_breaker.is_open() is False   # prober
    assert veo_breaker.is_open() is True    # stays open
    assert veo_breaker.is_open() is True


# --- half-open: only ONE probe per window ---
def test_half_open_lets_exactly_one_through(fake):
    for _ in range(3):
        veo_breaker.record_rate_limit()
    results = [veo_breaker.is_open() for _ in range(5)]
    # exactly one False (the prober), rest True
    assert results.count(False) == 1
    assert results.count(True) == 4


# --- recovery: a probe success closes the breaker for everyone ---
def test_success_closes_breaker(fake):
    for _ in range(3):
        veo_breaker.record_rate_limit()
    assert veo_breaker.is_open() is False  # prober goes
    veo_breaker.record_success()           # probe succeeded → close
    # closed for everyone now
    assert veo_breaker.is_open() is False
    assert veo_breaker.is_open() is False


def test_state_shape(fake):
    s = veo_breaker.state()
    assert s["enabled"] is True and s["open"] is False
    for _ in range(3):
        veo_breaker.record_rate_limit()
    s2 = veo_breaker.state()
    assert s2["open"] is True
