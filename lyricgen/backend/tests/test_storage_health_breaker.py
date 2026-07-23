import storage


class _FailingClient:
    def __init__(self):
        self.calls = 0

    def head_bucket(self, **_kwargs):
        self.calls += 1
        raise RuntimeError("r2 unavailable")


def _reset_breaker(monkeypatch, client):
    monkeypatch.setattr(storage, "_health_client", client)
    monkeypatch.setattr(storage, "_health_probe_failures", 0)
    monkeypatch.setattr(storage, "_health_circuit_open_until", 0.0)
    monkeypatch.setattr(storage, "_health_probe_last_result", None)
    monkeypatch.setattr(storage, "_health_probe_last_at", 0.0)
    monkeypatch.setattr(storage, "_health_probe_executor", None)
    monkeypatch.setattr(storage, "_health_probe_future", None)
    monkeypatch.setenv("R2_HEALTH_BREAKER_FAILURE_THRESHOLD", "2")
    monkeypatch.setenv("R2_HEALTH_BREAKER_COOLDOWN_SECONDS", "30")
    monkeypatch.setenv("R2_HEALTH_PROBE_CACHE_SECONDS", "0")


def test_r2_health_breaker_opens_and_stops_probe_fanout(monkeypatch):
    client = _FailingClient()
    _reset_breaker(monkeypatch, client)

    assert storage.probe_r2()[0] is False
    # A failed probe discards the client. Re-inject the fake as the next
    # health request would construct a fresh isolated boto client.
    monkeypatch.setattr(storage, "_health_client", client)
    assert storage.probe_r2()[0] is False
    assert storage.health_probe_state()["state"] == "open"

    ok, elapsed, error = storage.probe_r2()
    assert ok is False
    assert elapsed == 0
    assert error.startswith("circuit_open")
    assert client.calls == 2


def test_r2_health_breaker_success_resets_failures(monkeypatch):
    class HealthyClient:
        calls = 0

        def head_bucket(self, **_kwargs):
            self.calls += 1
            return None

    client = HealthyClient()
    _reset_breaker(monkeypatch, client)
    monkeypatch.setenv("R2_HEALTH_PROBE_CACHE_SECONDS", "5")
    monkeypatch.setattr(storage, "_health_probe_failures", 1)

    assert storage.probe_r2()[0] is True
    assert storage.probe_r2()[0] is True
    assert client.calls == 1
    assert storage.health_probe_state() == {
        "state": "closed",
        "failures": 0,
        "retry_after_seconds": 0,
        "probe_inflight": False,
    }


def test_hard_timeouts_never_accumulate_probe_threads(monkeypatch):
    from concurrent.futures import TimeoutError

    class NeverFinishes:
        def result(self, timeout=None):
            raise TimeoutError()

        def done(self):
            return False

    class CountingExecutor:
        def __init__(self):
            self.submits = 0

        def submit(self, *_args, **_kwargs):
            self.submits += 1
            return NeverFinishes()

        def shutdown(self, **_kwargs):
            raise AssertionError("a live timed-out probe must not be discarded")

    class Client:
        def head_bucket(self, **_kwargs):
            return None

    client = Client()
    executor = CountingExecutor()
    _reset_breaker(monkeypatch, client)
    monkeypatch.setenv("R2_HEALTH_BREAKER_FAILURE_THRESHOLD", "1")
    monkeypatch.setattr(storage, "_new_health_probe_executor", lambda: executor)

    assert storage.probe_r2()[0] is False
    for _ in range(5):
        # Simulate successive cooldowns expiring while the underlying SDK
        # call is still stuck.
        monkeypatch.setattr(storage, "_health_circuit_open_until", 0.0)
        monkeypatch.setattr(storage, "_health_probe_last_at", 0.0)
        assert storage.probe_r2()[0] is False

    assert executor.submits == 1
    assert storage.health_probe_state()["probe_inflight"] is True
