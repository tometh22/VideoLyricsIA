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


# ── Fase 1.5: past-due dunning state (in-app banner) ────────────────────────

def _set_customer(client, user_token, db, customer_id):
    """Give the test user a (unique) Stripe customer id so the webhook
    handlers, which resolve users by stripe_customer_id, can find them."""
    from database import User
    uid = client.get("/auth/me", headers=auth(user_token)).json()["id"]
    u = db.query(User).filter(User.id == uid).first()
    u.stripe_customer_id = customer_id
    db.commit()
    return u


def test_auth_me_defaults_billing_status_active(client, user_token):
    """A fresh user is in good standing — /auth/me exposes it for the banner."""
    me = client.get("/auth/me", headers=auth(user_token)).json()
    assert me["billing_status"] == "active"


def test_invoice_failed_sets_past_due_then_paid_recovers(client, user_token, db, monkeypatch):
    """A failed charge flips the user to past_due (banner shows); a later
    successful charge heals it back to active (banner clears)."""
    import billing
    from database import User
    monkeypatch.setattr(billing, "_send_email_async", lambda *a, **k: None)
    u = _set_customer(client, user_token, db, "cus_f15_failpaid")

    billing._handle_invoice_failed(db, {
        "customer": "cus_f15_failpaid", "id": "in_f15_a",
        "amount_due": 90000, "currency": "usd",
    })
    db.refresh(u)
    assert u.billing_status == "past_due"
    # Surfaced to the frontend through /auth/me (fresh from DB, no cache).
    assert client.get("/auth/me", headers=auth(user_token)).json()["billing_status"] == "past_due"

    billing._handle_invoice_paid(db, {
        "customer": "cus_f15_failpaid", "id": "in_f15_b",
        "amount_paid": 90000, "currency": "usd",
    })
    db.refresh(u)
    assert u.billing_status == "active"


def test_subscription_updated_status_drives_banner(client, user_token, db):
    """subscription.updated:past_due raises the banner; :active clears it —
    this is the Smart-Retries recovery path."""
    import billing
    u = _set_customer(client, user_token, db, "cus_f15_substatus")

    billing._handle_subscription_updated(db, {
        "customer": "cus_f15_substatus", "id": "sub_f15",
        "status": "past_due", "metadata": {},
    })
    db.refresh(u)
    assert u.billing_status == "past_due"

    billing._handle_subscription_updated(db, {
        "customer": "cus_f15_substatus", "id": "sub_f15",
        "status": "active", "metadata": {},
    })
    db.refresh(u)
    assert u.billing_status == "active"


def test_subscription_deleted_clears_past_due(client, user_token, db):
    """Grace period exhausted → Stripe cancels → user lands on free with NO
    lingering past-due banner."""
    import billing
    u = _set_customer(client, user_token, db, "cus_f15_deleted")
    u.billing_status = "past_due"
    u.stripe_subscription_id = "sub_del"
    db.commit()

    billing._handle_subscription_deleted(db, {
        "customer": "cus_f15_deleted", "id": "sub_del",
    })
    db.refresh(u)
    assert u.plan_id == "free"
    assert u.billing_status == "active"


def test_subscription_info_exposes_billing_status(client, user_token):
    """The Facturación tab reads billing_status off /billing/subscription."""
    res = client.get("/billing/subscription", headers=auth(user_token))
    assert res.status_code == 200
    assert res.json()["billing_status"] == "active"
