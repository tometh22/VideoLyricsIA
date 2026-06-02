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


# ── Fase 1: payment-failed email with retry date ────────────────────────────

def test_payment_failed_email_includes_retry_date(monkeypatch):
    import emails
    sent = {}
    monkeypatch.setattr(emails, "_send_email", lambda to, subj, body: sent.update(body=body, subj=subj))
    emails.send_payment_failed("u@test.com", "user", 9.0, "usd", retry_date="15 Jul 2026")
    assert "15 Jul 2026" in sent["body"]
    assert "retry" in sent["body"].lower()


def test_payment_failed_email_without_retry_date(monkeypatch):
    import emails
    sent = {}
    monkeypatch.setattr(emails, "_send_email", lambda to, subj, body: sent.update(body=body))
    emails.send_payment_failed("u@test.com", "user", 9.0, "usd")
    assert "update your payment method" in sent["body"].lower()


# ── Fase 1: downgrade guardrail ─────────────────────────────────────────────

def test_downgrade_guardrail_blocks_when_usage_over_target(client, user_token, db, monkeypatch):
    """A user on Plan 1000 who used 500 videos cannot drop to Plan 100 (limit
    100) mid-cycle — it would instantly block their generation. Expect 400."""
    import billing, auth as auth_mod
    from database import User
    monkeypatch.setattr(billing.stripe, "api_key", "sk_test_fake")
    monkeypatch.setitem(billing.PLANS["100"], "stripe_price_id", "price_fake_100")
    uid = client.get("/auth/me", headers=auth(user_token)).json()["id"]
    u = db.query(User).filter(User.id == uid).first()
    u.stripe_subscription_id = "sub_fake"
    u.plan_id = "1000"
    u.allow_overage = False
    db.commit()
    monkeypatch.setattr(auth_mod, "get_plan_usage",
                        lambda *a, **k: {"used": 500, "limit": 1000})
    res = client.post("/billing/change-plan", headers=auth(user_token),
                      json={"plan_id": "100"})
    assert res.status_code == 400, res.text
    assert "este mes" in res.json()["detail"]


def test_overage_account_can_still_change_plan(client, user_token, db, monkeypatch):
    """allow_overage accounts (UMG-style) are exempt from the guardrail — the
    block must not fire (it proceeds to Stripe, which we stub)."""
    import billing, auth as auth_mod
    from database import User
    monkeypatch.setattr(billing.stripe, "api_key", "sk_test_fake")
    monkeypatch.setitem(billing.PLANS["100"], "stripe_price_id", "price_fake_100")
    uid = client.get("/auth/me", headers=auth(user_token)).json()["id"]
    u = db.query(User).filter(User.id == uid).first()
    u.stripe_subscription_id = "sub_fake"
    u.plan_id = "1000"
    u.allow_overage = True
    db.commit()
    monkeypatch.setattr(auth_mod, "get_plan_usage",
                        lambda *a, **k: {"used": 500, "limit": 1000})
    _item = type("I", (), {"id": "si_1"})()
    monkeypatch.setattr(billing.stripe.Subscription, "retrieve",
                        lambda sid: {"items": {"data": [_item]}})
    monkeypatch.setattr(billing.stripe.Subscription, "modify", lambda *a, **k: None)
    res = client.post("/billing/change-plan", headers=auth(user_token),
                      json={"plan_id": "100"})
    assert res.status_code == 200, res.text
