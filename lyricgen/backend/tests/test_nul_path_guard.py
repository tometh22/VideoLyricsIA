"""NUL bytes in URL paths are rejected before any DB lookup."""

import asyncio


def _drive(middleware, path):
    state = {"inner_called": False}

    async def inner_app(scope, receive, send):
        state["inner_called"] = True

    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    asyncio.run(middleware(inner_app).__call__(
        {"type": "http", "path": path},
        receive,
        send,
    ))
    start = next(
        (message for message in sent
         if message.get("type") == "http.response.start"),
        None,
    )
    return state["inner_called"], start


def test_nul_in_path_returns_400_without_calling_the_app():
    from main import RejectNulPathMiddleware

    inner_called, start = _drive(RejectNulPathMiddleware, "/status/\x00")
    assert inner_called is False
    assert start is not None and start["status"] == 400


def test_clean_path_passes_through_untouched():
    from main import RejectNulPathMiddleware

    inner_called, start = _drive(
        RejectNulPathMiddleware,
        "/status/330bc837c3e8",
    )
    assert inner_called is True
    assert start is None
