"""CV4 (audit Sprint 2 2026-05-25) — Redis-backed counters tests.

Counters movidos de in-memory dict → Redis INCR. Con 2+ API replicas,
in-memory daba números inconsistentes entre replicas. Redis es atomic
+ cluster-wide.

Tests cubren:
1. Happy path con Redis (mock)
2. Fallback a in-memory cuando Redis falla
3. track_request + get_metrics simétricos
4. queue_jobs._bump_prewarm_counter idempotente + visible vía get_prewarm_counters
"""

import pytest
from unittest.mock import MagicMock


def test_track_request_uses_redis_when_available(monkeypatch):
    """Happy path: con Redis funcionando, INCR las 2 keys correctas."""
    import bg_preview

    mock_r = MagicMock()
    monkeypatch.setattr(bg_preview, "_redis_client", lambda: mock_r)

    bg_preview.track_request(cache_hit=True)
    # Debe haber 2 INCRs: requests_total + cache_hits.
    assert mock_r.incr.call_count == 2
    keys_called = [c.args[0] for c in mock_r.incr.call_args_list]
    assert bg_preview._REDIS_KEY_REQUESTS in keys_called
    assert bg_preview._REDIS_KEY_HITS in keys_called
    assert bg_preview._REDIS_KEY_MISSES not in keys_called


def test_track_request_miss_increments_misses(monkeypatch):
    import bg_preview

    mock_r = MagicMock()
    monkeypatch.setattr(bg_preview, "_redis_client", lambda: mock_r)

    bg_preview.track_request(cache_hit=False)
    keys_called = [c.args[0] for c in mock_r.incr.call_args_list]
    assert bg_preview._REDIS_KEY_REQUESTS in keys_called
    assert bg_preview._REDIS_KEY_MISSES in keys_called
    assert bg_preview._REDIS_KEY_HITS not in keys_called


def test_track_request_falls_back_to_inmem_when_redis_unavailable(monkeypatch):
    """Sin Redis (returns None), debe seguir contando en in-memory."""
    import bg_preview

    monkeypatch.setattr(bg_preview, "_redis_client", lambda: None)
    # Reset in-memory counters.
    with bg_preview._inmem_lock:
        bg_preview._inmem_metrics["requests_total"] = 0
        bg_preview._inmem_metrics["cache_hits"] = 0
        bg_preview._inmem_metrics["cache_misses"] = 0

    bg_preview.track_request(cache_hit=True)
    bg_preview.track_request(cache_hit=False)
    bg_preview.track_request(cache_hit=True)

    with bg_preview._inmem_lock:
        m = dict(bg_preview._inmem_metrics)
    assert m["requests_total"] == 3
    assert m["cache_hits"] == 2
    assert m["cache_misses"] == 1


def test_get_metrics_reads_redis_when_available(monkeypatch):
    import bg_preview

    mock_r = MagicMock()
    # Stub r.get(key) → string bytes (como Redis real devuelve).
    def fake_get(key):
        return {
            bg_preview._REDIS_KEY_REQUESTS: b"10",
            bg_preview._REDIS_KEY_HITS: b"7",
            bg_preview._REDIS_KEY_MISSES: b"3",
        }.get(key, b"0")
    mock_r.get.side_effect = fake_get
    monkeypatch.setattr(bg_preview, "_redis_client", lambda: mock_r)

    m = bg_preview.get_metrics()
    assert m["requests_total"] == 10
    assert m["cache_hits"] == 7
    assert m["cache_misses"] == 3
    assert m["cache_hit_rate"] == 0.7
    assert m["estimated_wasted_cost_usd"] == 4.5  # 3 * 1.50
    assert m["source"] == "redis"


def test_get_metrics_falls_back_inmem_when_redis_unavailable(monkeypatch):
    import bg_preview

    monkeypatch.setattr(bg_preview, "_redis_client", lambda: None)
    with bg_preview._inmem_lock:
        bg_preview._inmem_metrics["requests_total"] = 5
        bg_preview._inmem_metrics["cache_hits"] = 2
        bg_preview._inmem_metrics["cache_misses"] = 3

    m = bg_preview.get_metrics()
    assert m["requests_total"] == 5
    assert m["source"] == "inmem_fallback"


def test_get_metrics_handles_redis_get_failure(monkeypatch):
    """Si Redis ping OK pero get() falla → fallback graceful."""
    import bg_preview

    mock_r = MagicMock()
    mock_r.get.side_effect = Exception("redis timeout mid-call")
    monkeypatch.setattr(bg_preview, "_redis_client", lambda: mock_r)

    with bg_preview._inmem_lock:
        bg_preview._inmem_metrics["requests_total"] = 99

    m = bg_preview.get_metrics()
    assert m["source"] == "inmem_fallback"
    # Tomó valores del inmem ya que Redis falló.
    assert m["requests_total"] == 99


def test_prewarm_counter_uses_redis_when_available(monkeypatch):
    import queue_jobs

    mock_r = MagicMock()
    monkeypatch.setattr(
        queue_jobs, "_init_redis",
        lambda: (mock_r, None, None),
    )

    queue_jobs._bump_prewarm_counter(skipped=True)
    queue_jobs._bump_prewarm_counter(skipped=False)
    queue_jobs._bump_prewarm_counter(skipped=False)

    # 3 calls a r.incr
    assert mock_r.incr.call_count == 3
    keys_called = [c.args[0] for c in mock_r.incr.call_args_list]
    assert keys_called.count(queue_jobs._PREWARM_REDIS_KEY_SKIPPED) == 1
    assert keys_called.count(queue_jobs._PREWARM_REDIS_KEY_ENQUEUED) == 2


def test_prewarm_counter_falls_back_inmem(monkeypatch):
    import queue_jobs

    # Reset
    queue_jobs.prewarm_skipped_total = 0
    queue_jobs.prewarm_enqueued_total = 0

    monkeypatch.setattr(
        queue_jobs, "_init_redis", lambda: (None, None, None),
    )
    queue_jobs._bump_prewarm_counter(skipped=True)
    queue_jobs._bump_prewarm_counter(skipped=False)
    queue_jobs._bump_prewarm_counter(skipped=True)

    assert queue_jobs.prewarm_skipped_total == 2
    assert queue_jobs.prewarm_enqueued_total == 1


def test_get_prewarm_counters_reads_redis(monkeypatch):
    import queue_jobs

    mock_r = MagicMock()
    mock_r.get.side_effect = lambda k: b"42" if "skipped" in k else b"100"
    monkeypatch.setattr(
        queue_jobs, "_init_redis", lambda: (mock_r, None, None),
    )

    snap = queue_jobs.get_prewarm_counters()
    assert snap["skipped"] == 42
    assert snap["enqueued"] == 100
    assert snap["source"] == "redis"


def test_get_prewarm_counters_inmem_fallback(monkeypatch):
    import queue_jobs

    queue_jobs.prewarm_skipped_total = 5
    queue_jobs.prewarm_enqueued_total = 10
    monkeypatch.setattr(
        queue_jobs, "_init_redis", lambda: (None, None, None),
    )

    snap = queue_jobs.get_prewarm_counters()
    assert snap["skipped"] == 5
    assert snap["enqueued"] == 10
    assert snap["source"] == "inmem_fallback"
