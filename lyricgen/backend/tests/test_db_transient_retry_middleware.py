import asyncio

import pytest
from sqlalchemy.exc import OperationalError

from main import DbTransientRetryMiddleware


def transient_error():
    return OperationalError(
        "SELECT 1", {}, Exception("SSL connection has been closed unexpectedly"),
    )


def bad_record_mac_error():
    return OperationalError(
        "SELECT 1", {}, Exception("SSL error: tlsv1 alert bad record mac"),
    )


def test_bodyless_editor_heartbeat_is_retried_once(monkeypatch):
    calls = 0
    received = []
    sent = []

    async def inner_app(_scope, receive, send):
        nonlocal calls
        calls += 1
        received.append(await receive())
        if calls == 1:
            raise transient_error()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    async def original_receive():
        raise AssertionError("bodyless heartbeat must use a replayable empty body")

    async def capture_send(message):
        sent.append(message)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("main.asyncio.sleep", no_sleep)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/editor/159a79e3921b/lock/heartbeat",
        "headers": [],
    }

    asyncio.run(DbTransientRetryMiddleware(inner_app)(scope, original_receive, capture_send))

    assert calls == 2
    assert received == [
        {"type": "http.request", "body": b"", "more_body": False},
        {"type": "http.request", "body": b"", "more_body": False},
    ]
    assert sent[0]["status"] == 200


def test_unknown_bodyless_post_is_not_retried():
    calls = 0

    async def inner_app(_scope, _receive, _send):
        nonlocal calls
        calls += 1
        raise transient_error()

    async def original_receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def ignore_send(_message):
        return None

    async def run():
        middleware = DbTransientRetryMiddleware(inner_app)
        await middleware({
            "type": "http", "method": "POST", "path": "/unknown", "headers": [],
        }, original_receive, ignore_send)

    with pytest.raises(OperationalError):
        asyncio.run(run())

    assert calls == 1


def test_get_retries_bad_record_mac_and_returns_503_contract(monkeypatch):
    calls = 0
    sent = []

    async def inner_app(_scope, receive, _send):
        nonlocal calls
        calls += 1
        assert (await receive())["type"] == "http.request"
        raise bad_record_mac_error()

    original_calls = 0

    async def original_receive():
        nonlocal original_calls
        original_calls += 1
        return {"type": "http.request", "body": b"", "more_body": False}

    async def capture_send(message):
        sent.append(message)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("main.asyncio.sleep", no_sleep)
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/jobs/d4b22e7ed4b2/source-audio-url",
        "headers": [],
    }

    asyncio.run(DbTransientRetryMiddleware(inner_app)(scope, original_receive, capture_send))

    assert calls == 3
    assert original_calls == 1
    start = next(message for message in sent if message["type"] == "http.response.start")
    assert start["status"] == 503
    assert (b"retry-after", b"2") in start["headers"]
