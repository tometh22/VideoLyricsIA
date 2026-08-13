"""Tier 1 — Railway-blip resilience hardening.

These tests lock in the *config wiring* of three fail-fast changes (they do
NOT simulate a real network blip — they assert the timeouts/keepalives are
actually wired, which is the thing that regressed-by-omission historically):

  - C4  database._build_pg_connect_args  → connect_timeout on Postgres
  - M1  worker._redis_connect_kwargs      → connect timeout + TCP keepalive,
        and crucially NO short socket_timeout (would break the BLPOP dequeue)
  - H4  pipeline OAuth refresh            → bounded-timeout requests.Session

Why assert wiring rather than behavior: each fix is a single kwarg whose
absence reintroduces an indefinite hang during a Railway private-networking
blip. A cheap presence test is the right guard against an accidental revert.
"""

import importlib
import os


# --------------------------------------------------------------------------
# C4 — DB connect_timeout
# --------------------------------------------------------------------------
def test_pg_connect_args_have_connect_timeout():
    import database
    args = database._build_pg_connect_args()
    assert args["connect_timeout"] == 5, "default DB connect_timeout must be 5s"
    # keepalives preserved (must not regress the existing zombie-session fix)
    assert args["keepalives"] == 1
    assert args["keepalives_idle"] == 30


def test_pg_connect_timeout_env_override(monkeypatch):
    monkeypatch.setenv("DB_CONNECT_TIMEOUT", "3")
    import database
    importlib.reload(database)
    try:
        assert database._build_pg_connect_args()["connect_timeout"] == 3
    finally:
        monkeypatch.delenv("DB_CONNECT_TIMEOUT", raising=False)
        importlib.reload(database)


# --------------------------------------------------------------------------
# M1 — worker Redis socket resilience
# --------------------------------------------------------------------------
def test_worker_redis_kwargs_bound_connect_and_keepalive():
    import worker
    kw = worker._redis_connect_kwargs()
    assert kw["socket_connect_timeout"] == 5
    assert kw["socket_keepalive"] is True
    assert "socket_keepalive_options" in kw
    assert kw["health_check_interval"] == 30


def test_worker_redis_has_no_short_socket_timeout():
    """A short socket_timeout on the worker would fire on every idle BLPOP and
    break dequeuing — the regression we must never reintroduce."""
    import worker
    kw = worker._redis_connect_kwargs()
    assert "socket_timeout" not in kw


def test_worker_redis_keepalive_options_present_on_linux():
    """On Linux (prod) the TCP_KEEP* tuning must be populated; on macOS dev
    the getattr guard yields an empty dict (OS-default keepalive) and that's
    acceptable — assert the dict exists and is well-formed either way."""
    import socket
    import worker
    opts = worker._redis_connect_kwargs()["socket_keepalive_options"]
    assert isinstance(opts, dict)
    if hasattr(socket, "TCP_KEEPIDLE"):
        assert opts[socket.TCP_KEEPIDLE] == 30
        assert opts[socket.TCP_KEEPINTVL] == 10
        assert opts[socket.TCP_KEEPCNT] == 3


# --------------------------------------------------------------------------
# H4 — OAuth refresh bounded timeout
# --------------------------------------------------------------------------
def test_oauth_timeout_session_injects_default_timeout(monkeypatch):
    import requests
    import pipeline

    captured = {}

    def _fake_request(self, *args, **kwargs):
        captured.update(kwargs)

        class _Resp:
            status_code = 200

        return _Resp()

    monkeypatch.setattr(requests.Session, "request", _fake_request)
    session = pipeline._make_timeout_session(7.5)
    session.get("http://example.invalid/token")
    assert captured.get("timeout") == 7.5


def test_oauth_caps_googleauth_explicit_default(monkeypatch):
    """THE regression guard: google-auth's transport ALWAYS passes an explicit
    timeout (its _DEFAULT_TIMEOUT=120) to session.request — a `setdefault` would
    be a no-op and the refresh would still hang 120s. The session must CAP it to
    our bound. This drives the real google-auth call shape (explicit timeout)."""
    import requests
    import pipeline

    captured = {}
    monkeypatch.setattr(
        requests.Session, "request",
        lambda self, *a, **kw: captured.update(kw) or type("R", (), {"status_code": 200})(),
    )
    session = pipeline._make_timeout_session(7.5)
    session.get("http://example.invalid/token", timeout=120)  # what google-auth passes
    assert captured.get("timeout") == 7.5, "must cap google-auth's 120s default to our bound"


def test_oauth_does_not_extend_a_shorter_explicit_timeout(monkeypatch):
    """Cap only shortens — a deliberately-shorter explicit timeout is preserved."""
    import requests
    import pipeline

    captured = {}
    monkeypatch.setattr(
        requests.Session, "request",
        lambda self, *a, **kw: captured.update(kw) or type("R", (), {"status_code": 200})(),
    )
    session = pipeline._make_timeout_session(7.5)
    session.get("http://example.invalid/token", timeout=2)
    assert captured.get("timeout") == 2


def test_oauth_refresh_request_is_bound_to_a_session():
    import pipeline
    req = pipeline._oauth_refresh_request()
    # google-auth Request carries the session we handed it; presence of a
    # non-default session is what guarantees the bounded timeout applies.
    assert getattr(req, "session", None) is not None


def test_oauth_refresh_timeout_default():
    import pipeline
    assert pipeline._OAUTH_REFRESH_TIMEOUT == 10.0
