"""Tests for billing endpoints."""

from tests.conftest import auth


def test_subscription_info(client, user_token):
    res = client.get("/billing/subscription", headers=auth(user_token))
    assert res.status_code == 200
    data = res.json()
    assert data["plan"] == "free"
    assert data["has_subscription"] is False


def test_invoices_empty(client, user_token):
    res = client.get("/billing/invoices", headers=auth(user_token))
    assert res.status_code == 200
    assert res.json() == []


def test_checkout_no_stripe(client, user_token):
    """Checkout should fail gracefully when Stripe is not configured."""
    res = client.post("/billing/checkout", headers=auth(user_token), json={
        "plan_id": "100",
    })
    # Should return 503 (billing not configured) since no STRIPE_SECRET_KEY
    assert res.status_code == 503


def test_portal_no_customer(client, user_token):
    """Portal should fail when user has no Stripe customer."""
    res = client.post("/billing/portal", headers=auth(user_token))
    # Should return 503 (no stripe key) or 400 (no customer)
    assert res.status_code in (400, 503)


def test_checkout_invalid_plan(client, user_token):
    res = client.post("/billing/checkout", headers=auth(user_token), json={
        "plan_id": "nonexistent",
    })
    assert res.status_code in (400, 503)
