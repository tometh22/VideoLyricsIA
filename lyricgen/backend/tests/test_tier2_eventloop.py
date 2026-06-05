"""Tier 2 — async/event-loop unblocking regression guards.

These lock in the *concurrency model* of the Tier 2 change so an accidental
re-add of `async` (which would put blocking DB/Stripe work back on the event
loop) is caught by CI. They assert which handlers are sync vs async — NOT
behavior (behavior is covered by the existing auth/billing/status suites,
which must stay green because the logic is unchanged).

Why this matters: a handler that does only sync DB work must be plain `def`
(FastAPI runs it in the threadpool, off the loop). The reviewer's eye can't
reliably catch an `async def` creeping back onto a 10k-line file; this test can.
"""

import inspect

import pytest


# Handlers in main.py that MUST be sync `def` (converted from async; no await).
_MAIN_SYNC_HANDLERS = [
    "status", "list_jobs", "usage", "telemetry_heartbeat", "transcription_status",
    "me", "refresh_token", "get_settings", "save_settings", "drive_status",
    "get_drive_transfer", "list_backgrounds", "background_usage", "admin_queue",
    "get_delivery_profiles", "list_fonts",
]

# Billing handlers that MUST be sync `def` (Stripe+DB offloaded to threadpool).
_BILLING_SYNC_HANDLERS = [
    "create_checkout_session", "create_portal_session", "change_plan",
    "change_plan_preview", "cancel_subscription", "reactivate_subscription",
    "list_invoices", "get_subscription", "get_payment_method",
]


def test_get_current_user_is_sync():
    """The linchpin: runs on every authenticated request. Must be plain `def`
    so its blocking DB queries run in the threadpool, not on the event loop."""
    import auth
    assert not inspect.iscoroutinefunction(auth.get_current_user), (
        "get_current_user must be plain `def` (sync) — re-adding `async` puts "
        "1-2 blocking DB queries back on the event loop for every request."
    )


@pytest.mark.parametrize("name", _MAIN_SYNC_HANDLERS)
def test_main_hot_handlers_are_sync(name):
    import main
    fn = getattr(main, name)
    assert not inspect.iscoroutinefunction(fn), (
        f"main.{name} must be plain `def` (sync) — it does only blocking DB "
        f"work and must run in the threadpool, not on the event loop."
    )


def test_sse_handler_stays_async_with_sync_tick_helper():
    """SSE must STAY async (it awaits sleep + yields), but its per-tick blocking
    work must live in the sync `_sse_tick` helper (run via asyncio.to_thread)."""
    import main
    assert inspect.iscoroutinefunction(main.job_events), (
        "job_events must stay `async def` (it awaits asyncio.sleep and yields)."
    )
    assert hasattr(main, "_sse_tick"), "_sse_tick helper must exist"
    assert not inspect.iscoroutinefunction(main._sse_tick), (
        "_sse_tick must be a plain sync function (it's run via asyncio.to_thread)."
    )


@pytest.mark.parametrize("name", _BILLING_SYNC_HANDLERS)
def test_billing_handlers_are_sync(name):
    import billing
    fn = getattr(billing, name)
    assert not inspect.iscoroutinefunction(fn), (
        f"billing.{name} must be plain `def` — its blocking Stripe+DB calls run "
        f"in the threadpool. Re-adding `async` freezes the loop on a slow Stripe."
    )


def test_stripe_webhook_stays_async_with_sync_dispatch():
    """Webhook awaits request.body() so it stays async, but the DB-heavy event
    dispatch must be in the sync `_dispatch_webhook_event` helper (to_thread)."""
    import billing
    assert inspect.iscoroutinefunction(billing.stripe_webhook), (
        "stripe_webhook must stay async (it awaits request.body())."
    )
    assert hasattr(billing, "_dispatch_webhook_event")
    assert not inspect.iscoroutinefunction(billing._dispatch_webhook_event), (
        "_dispatch_webhook_event must be sync (run via asyncio.to_thread)."
    )


def test_stripe_max_network_retries_configured():
    import billing  # noqa: F401 — import configures stripe at module load
    import stripe
    assert stripe.max_network_retries == 2, (
        "Stripe max_network_retries must be set (default 2) so a transient "
        "Stripe blip retries instead of surfacing a hard failure."
    )
