"""RejectNulPathMiddleware — a NUL byte in the URL path is rejected at the
edge with a 400, before it can reach a DB lookup and blow up as an unhandled
500 (`ValueError: A string literal cannot contain NUL (0x00) characters`).

Tested at the ASGI contract level (mock scope/receive/send) so it needs no DB
or network — just the middleware's own behaviour.
"""
import asyncio


def _drive(middleware, path):
    """Run the middleware over one HTTP request and return
    (inner_called, response_start_message_or_None)."""
    state = {"inner_called": False}

    async def inner_app(scope, receive, send):
        state["inner_called"] = True

    sent = []

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    asyncio.run(middleware(inner_app).__call__(
        {"type": "http", "path": path}, receive, send,
    ))
    start = next((m for m in sent if m.get("type") == "http.response.start"), None)
    return state["inner_called"], start


def test_nul_in_path_returns_400_and_never_hits_app():
    from main import RejectNulPathMiddleware
    inner_called, start = _drive(RejectNulPathMiddleware, "/status/\x00")
    assert inner_called is False, "request with NUL must not reach the app"
    assert start is not None and start["status"] == 400


def test_clean_path_passes_through_untouched():
    from main import RejectNulPathMiddleware
    inner_called, start = _drive(RejectNulPathMiddleware, "/status/330bc837c3e8")
    assert inner_called is True, "well-formed path must reach the app"
    assert start is None, "middleware must not synthesise a response for clean paths"
