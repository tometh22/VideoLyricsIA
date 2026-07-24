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


# ── Fase 2 / PR1: explicit Stripe Customer Portal configuration ──────────────

def test_portal_config_params_omits_plan_switching():
    """Founder decision: plan changes stay in-app behind the Fase 1 guardrail,
    so the hosted portal must NOT offer subscription_update."""
    import billing
    p = billing.build_portal_configuration_params()
    f = p["features"]
    assert "subscription_update" not in f
    assert set(f) == {"payment_method_update", "invoice_history", "subscription_cancel"}
    assert f["subscription_cancel"]["mode"] == "at_period_end"
    assert f["subscription_cancel"]["proration_behavior"] == "none"
    assert p["metadata"]["genly_marker"] == billing._PORTAL_CONFIG_MARKER


def test_ensure_portal_config_prefers_pinned_env(monkeypatch):
    """A pinned STRIPE_PORTAL_CONFIG_ID short-circuits — zero Stripe calls."""
    import billing
    monkeypatch.setattr(billing, "STRIPE_PORTAL_CONFIG_ID", "bpc_pinned")
    monkeypatch.setattr(billing, "_portal_config_id_cache", None)
    calls = {"n": 0}
    def _boom(**k): calls.__setitem__("n", calls["n"] + 1); raise AssertionError("no Stripe call")
    monkeypatch.setattr(billing.stripe.billing_portal.Configuration, "list", _boom)
    monkeypatch.setattr(billing.stripe.billing_portal.Configuration, "create", _boom)
    assert billing._ensure_portal_configuration() == "bpc_pinned"
    assert calls["n"] == 0


def test_ensure_portal_config_reuses_by_marker_and_caches(monkeypatch):
    import billing
    monkeypatch.setattr(billing, "STRIPE_PORTAL_CONFIG_ID", "")
    monkeypatch.setattr(billing, "_portal_config_id_cache", None)

    class _Cfg:
        def __init__(self, id, marker, active=True):
            self.id = id; self.metadata = {"genly_marker": marker}; self.active = active
    class _List:
        def auto_paging_iter(self):
            return iter([_Cfg("bpc_other", "someone-else"),
                         _Cfg("bpc_ours", billing._PORTAL_CONFIG_MARKER)])
    created = {"n": 0}
    monkeypatch.setattr(billing.stripe.billing_portal.Configuration, "list", lambda **k: _List())
    monkeypatch.setattr(billing.stripe.billing_portal.Configuration, "create",
                        lambda **k: created.__setitem__("n", created["n"] + 1))
    assert billing._ensure_portal_configuration() == "bpc_ours"
    assert created["n"] == 0
    # cached → second call must not touch Stripe at all
    monkeypatch.setattr(billing.stripe.billing_portal.Configuration, "list",
                        lambda **k: (_ for _ in ()).throw(AssertionError("should be cached")))
    assert billing._ensure_portal_configuration() == "bpc_ours"


def test_ensure_portal_config_creates_once_when_absent(monkeypatch):
    import billing
    monkeypatch.setattr(billing, "STRIPE_PORTAL_CONFIG_ID", "")
    monkeypatch.setattr(billing, "_portal_config_id_cache", None)

    class _List:
        def auto_paging_iter(self): return iter([])
    class _New:
        id = "bpc_new"
    monkeypatch.setattr(billing.stripe.billing_portal.Configuration, "list", lambda **k: _List())
    monkeypatch.setattr(billing.stripe.billing_portal.Configuration, "create", lambda **k: _New())
    assert billing._ensure_portal_configuration() == "bpc_new"


def test_ensure_portal_config_none_on_stripe_error(monkeypatch):
    """A Stripe outage must NOT 500 /portal — ensure returns None so the
    caller falls back to the account default."""
    import billing
    monkeypatch.setattr(billing, "STRIPE_PORTAL_CONFIG_ID", "")
    monkeypatch.setattr(billing, "_portal_config_id_cache", None)
    def _boom(**k): raise billing.stripe.error.StripeError("down")
    monkeypatch.setattr(billing.stripe.billing_portal.Configuration, "list", _boom)
    assert billing._ensure_portal_configuration() is None


def test_portal_session_omits_configuration_when_none(client, user_token, db, monkeypatch):
    """When ensure returns None, Session.create must NOT receive configuration=None
    (Stripe rejects an explicit null) — the kwarg is absent entirely."""
    import billing
    from database import User
    monkeypatch.setattr(billing.stripe, "api_key", "sk_test_fake")
    monkeypatch.setattr(billing, "_ensure_portal_configuration", lambda: None)
    uid = client.get("/auth/me", headers=auth(user_token)).json()["id"]
    u = db.query(User).filter(User.id == uid).first()
    u.stripe_customer_id = "cus_portal_none"; db.commit()
    captured = {}
    class _S: url = "https://portal.test/x"
    monkeypatch.setattr(billing.stripe.billing_portal.Session, "create",
                        lambda **k: captured.update(k) or _S())
    res = client.post("/billing/portal", headers=auth(user_token))
    assert res.status_code == 200, res.text
    assert "configuration" not in captured
    assert res.json()["portal_url"] == "https://portal.test/x"


def test_portal_session_passes_configuration_when_present(client, user_token, db, monkeypatch):
    import billing
    from database import User
    monkeypatch.setattr(billing.stripe, "api_key", "sk_test_fake")
    monkeypatch.setattr(billing, "_ensure_portal_configuration", lambda: "bpc_x")
    uid = client.get("/auth/me", headers=auth(user_token)).json()["id"]
    u = db.query(User).filter(User.id == uid).first()
    u.stripe_customer_id = "cus_portal_cfg"; db.commit()
    captured = {}
    class _S: url = "https://portal.test/y"
    monkeypatch.setattr(billing.stripe.billing_portal.Session, "create",
                        lambda **k: captured.update(k) or _S())
    res = client.post("/billing/portal", headers=auth(user_token))
    assert res.status_code == 200, res.text
    assert captured.get("configuration") == "bpc_x"


# ── Fase 2 / PR2: read-only default payment method ──────────────────────────

def test_payment_method_null_when_stripe_unconfigured(client, user_token):
    res = client.get("/billing/payment-method", headers=auth(user_token))
    assert res.status_code == 200
    assert res.json() == {"payment_method": None}


def test_payment_method_returns_default_card(client, user_token, db, monkeypatch):
    """Resolves the canonical default via invoice_settings and returns only
    brand/last4/exp — never a PAN."""
    import billing
    from database import User
    monkeypatch.setattr(billing.stripe, "api_key", "sk_test_fake")
    uid = client.get("/auth/me", headers=auth(user_token)).json()["id"]
    u = db.query(User).filter(User.id == uid).first()
    u.stripe_customer_id = "cus_pm_card"; db.commit()

    class _Card:
        brand = "visa"; last4 = "4242"; exp_month = 8; exp_year = 2027
    class _PM:
        card = _Card()
    class _Inv:
        default_payment_method = _PM()
    class _Cust:
        invoice_settings = _Inv()
        default_source = None
    monkeypatch.setattr(billing.stripe.Customer, "retrieve", lambda *a, **k: _Cust())
    res = client.get("/billing/payment-method", headers=auth(user_token))
    assert res.status_code == 200
    assert res.json()["payment_method"] == {
        "brand": "visa", "last4": "4242", "exp_month": 8, "exp_year": 2027,
    }


def test_payment_method_null_on_stripe_error(client, user_token, db, monkeypatch):
    import billing
    from database import User
    monkeypatch.setattr(billing.stripe, "api_key", "sk_test_fake")
    uid = client.get("/auth/me", headers=auth(user_token)).json()["id"]
    u = db.query(User).filter(User.id == uid).first()
    u.stripe_customer_id = "cus_pm_err"; db.commit()
    def _boom(*a, **k): raise billing.stripe.error.StripeError("down")
    monkeypatch.setattr(billing.stripe.Customer, "retrieve", _boom)
    res = client.get("/billing/payment-method", headers=auth(user_token))
    assert res.status_code == 200
    assert res.json()["payment_method"] is None


# ── Fase 2 / PR3: lifecycle emails + webhook triggers ───────────────────────

def test_email_subscription_welcome_content(monkeypatch):
    import emails
    sent = {}
    monkeypatch.setattr(emails, "_send_email", lambda to, subj, body: sent.update(subj=subj, body=body))
    emails.send_subscription_welcome("u@t.com", "Agus", "250", 250, 2000)
    assert "Welcome to Plan 250" in sent["subj"]
    assert "250" in sent["body"] and "$2,000/mo" in sent["body"]


def test_email_plan_changed_direction(monkeypatch):
    import emails
    sent = {}
    monkeypatch.setattr(emails, "_send_email", lambda to, subj, body: sent.update(body=body))
    emails.send_plan_changed("u@t.com", "Agus", "100", "500", 500, "upgrade")
    assert "upgraded" in sent["body"].lower()
    emails.send_plan_changed("u@t.com", "Agus", "500", "100", 100, "downgrade")
    assert "credited" in sent["body"].lower()


def test_email_cancelled_with_and_without_date(monkeypatch):
    import emails
    sent = {}
    monkeypatch.setattr(emails, "_send_email", lambda to, subj, body: sent.update(body=body))
    emails.send_subscription_cancelled("u@t.com", "Agus", access_until="30 Jun 2026")
    assert "30 Jun 2026" in sent["body"]
    emails.send_subscription_cancelled("u@t.com", "Agus")
    assert "moved to the free plan" in sent["body"].lower()


def test_invoice_paid_welcome_only_on_subscription_create(client, user_token, db, monkeypatch):
    """First invoice (subscription_create) → welcome + receipt; recurring
    (subscription_cycle) → receipt only, never a second welcome."""
    import billing
    calls = []
    monkeypatch.setattr(billing, "_send_email_async", lambda fn, **k: calls.append(fn.__name__))
    u = _set_customer(client, user_token, db, "cus_welcome")
    u.plan_id = "250"; db.commit()
    billing._handle_invoice_paid(db, {"customer": "cus_welcome", "id": "in_w1",
        "amount_paid": 200000, "currency": "usd", "billing_reason": "subscription_create"})
    assert "send_invoice_paid" in calls and "send_subscription_welcome" in calls
    calls.clear()
    billing._handle_invoice_paid(db, {"customer": "cus_welcome", "id": "in_w2",
        "amount_paid": 200000, "currency": "usd", "billing_reason": "subscription_cycle"})
    assert "send_invoice_paid" in calls and "send_subscription_welcome" not in calls


def test_subscription_updated_plan_change_email(client, user_token, db, monkeypatch):
    import billing
    calls = []
    monkeypatch.setattr(billing, "_send_email_async", lambda fn, **k: calls.append((fn.__name__, k)))
    u = _set_customer(client, user_token, db, "cus_planchg")
    u.plan_id = "100"; db.commit()
    billing._handle_subscription_updated(db, {"customer": "cus_planchg", "id": "sub_pc",
        "status": "active", "metadata": {"plan_id": "500"}})
    assert any(c[0] == "send_plan_changed" and c[1]["direction"] == "upgrade" for c in calls)


def test_subscription_updated_no_plan_email_from_free(client, user_token, db, monkeypatch):
    """free → X is the initial subscribe (welcome owns it), not a plan change."""
    import billing
    calls = []
    monkeypatch.setattr(billing, "_send_email_async", lambda fn, **k: calls.append(fn.__name__))
    u = _set_customer(client, user_token, db, "cus_freeup")
    u.plan_id = "free"; db.commit()
    billing._handle_subscription_updated(db, {"customer": "cus_freeup", "id": "sub_fu",
        "status": "active", "metadata": {"plan_id": "100"}})
    assert "send_plan_changed" not in calls


def test_subscription_deleted_sends_cancellation_email(client, user_token, db, monkeypatch):
    import billing
    calls = []
    monkeypatch.setattr(billing, "_send_email_async", lambda fn, **k: calls.append((fn.__name__, k)))
    u = _set_customer(client, user_token, db, "cus_cancel_em")
    u.plan_id = "250"; u.stripe_subscription_id = "sub_c"; db.commit()
    billing._handle_subscription_deleted(db, {"customer": "cus_cancel_em",
        "id": "sub_c", "current_period_end": 1782000000})
    cancelled = [c for c in calls if c[0] == "send_subscription_cancelled"]
    assert cancelled and cancelled[0][1]["access_until"]


# ── Fase 2 / PR4: cancel / reactivate / proration preview ───────────────────

def test_change_plan_preview_returns_proration(client, user_token, db, monkeypatch):
    """Sums only the proration line at our pinned date — not next cycle's full
    charge."""
    import billing, auth as auth_mod
    from database import User
    monkeypatch.setattr(billing.stripe, "api_key", "sk_test_fake")
    monkeypatch.setitem(billing.PLANS["500"], "stripe_price_id", "price_fake_500")
    monkeypatch.setattr(auth_mod, "get_plan_usage", lambda *a, **k: {"used": 10, "limit": 100})
    uid = client.get("/auth/me", headers=auth(user_token)).json()["id"]
    u = db.query(User).filter(User.id == uid).first()
    u.plan_id = "100"; u.stripe_subscription_id = "sub_x"; u.stripe_customer_id = "cus_prev"; db.commit()
    _item = type("I", (), {"id": "si_1"})()
    monkeypatch.setattr(billing.stripe.Subscription, "retrieve",
                        lambda sid: {"items": {"data": [_item]}})
    def _upcoming(**kw):
        pd = kw.get("subscription_proration_date")
        return {"lines": {"data": [
            {"proration": True, "period": {"start": pd}, "amount": 1500},
            {"proration": False, "period": {"start": 0}, "amount": 50000},
        ]}, "currency": "usd", "amount_due": 1500}
    monkeypatch.setattr(billing.stripe.Invoice, "upcoming", _upcoming)
    res = client.post("/billing/change-plan/preview", headers=auth(user_token), json={"plan_id": "500"})
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["proration_cents"] == 1500 and data["currency"] == "usd"


def test_change_plan_preview_downgrade_blocked(client, user_token, db, monkeypatch):
    """The preview surfaces the SAME Fase-1 guardrail 400 as /change-plan."""
    import billing, auth as auth_mod
    from database import User
    monkeypatch.setattr(billing.stripe, "api_key", "sk_test_fake")
    monkeypatch.setitem(billing.PLANS["100"], "stripe_price_id", "price_fake_100")
    monkeypatch.setattr(auth_mod, "get_plan_usage", lambda *a, **k: {"used": 500, "limit": 1000})
    uid = client.get("/auth/me", headers=auth(user_token)).json()["id"]
    u = db.query(User).filter(User.id == uid).first()
    u.plan_id = "1000"; u.stripe_subscription_id = "sub_y"; u.allow_overage = False; db.commit()
    res = client.post("/billing/change-plan/preview", headers=auth(user_token), json={"plan_id": "100"})
    assert res.status_code == 400
    assert "este mes" in res.json()["detail"]


def test_cancel_sets_cancel_at_period_end(client, user_token, db, monkeypatch):
    import billing
    from database import User
    monkeypatch.setattr(billing.stripe, "api_key", "sk_test_fake")
    calls = []
    monkeypatch.setattr(billing, "_send_email_async", lambda fn, **k: calls.append(fn.__name__))
    uid = client.get("/auth/me", headers=auth(user_token)).json()["id"]
    u = db.query(User).filter(User.id == uid).first()
    u.stripe_subscription_id = "sub_cxl"; db.commit()
    captured = {}
    def _modify(sid, **kw): captured.update(kw); return type("S", (), {"current_period_end": 1782000000})()
    monkeypatch.setattr(billing.stripe.Subscription, "modify", _modify)
    res = client.post("/billing/cancel", headers=auth(user_token))
    assert res.status_code == 200, res.text
    assert captured["cancel_at_period_end"] is True
    assert res.json()["cancel_at_period_end"] is True
    assert "send_cancellation_scheduled" in calls


def test_reactivate_clears_cancel_at_period_end(client, user_token, db, monkeypatch):
    import billing
    from database import User
    monkeypatch.setattr(billing.stripe, "api_key", "sk_test_fake")
    uid = client.get("/auth/me", headers=auth(user_token)).json()["id"]
    u = db.query(User).filter(User.id == uid).first()
    u.stripe_subscription_id = "sub_re"; db.commit()
    captured = {}
    def _modify(sid, **kw): captured.update(kw); return type("S", (), {"current_period_end": 1782000000})()
    monkeypatch.setattr(billing.stripe.Subscription, "modify", _modify)
    res = client.post("/billing/reactivate", headers=auth(user_token))
    assert res.status_code == 200, res.text
    assert captured["cancel_at_period_end"] is False
