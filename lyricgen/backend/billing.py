"""Stripe billing integration for GenLy AI."""

import os
import logging
from datetime import datetime, timezone
from typing import Optional

import threading

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

import emails
from auth import get_current_user, PLANS, create_token
from database import User, Invoice, get_db

logger = logging.getLogger("genly.billing")


def _send_email_async(fn, **kwargs):
    """Spawn a daemon thread to send an email; log failures instead of
    swallowing them silently."""
    def _target():
        try:
            fn(**kwargs)
        except Exception as exc:
            logger.error("billing email send failed (%s): %s", fn.__name__, exc, exc_info=True)
    threading.Thread(target=_target, daemon=True).start()

# --- Stripe config ---
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:5173")

# Refuse to accept unsigned webhook payloads when billing is wired up: an
# unverified handler lets anyone POST a forged subscription update and grant
# themselves any plan (or hijack another user's billing identity via
# metadata.user_id).
_REQUIRE_WEBHOOK_SIGNATURE = bool(stripe.api_key)

# --- Customer Portal configuration ---
# Prefer an explicitly pinned configuration id (set in prod via the output
# of setup_stripe.py). When unset we lazily create-once and cache the id
# in-process so the hosted portal always exposes the exact feature set we
# intend (update card / invoice history / cancel-at-period-end / plan
# switching among OUR prices) instead of Stripe's opaque dashboard default.
STRIPE_PORTAL_CONFIG_ID = os.environ.get("STRIPE_PORTAL_CONFIG_ID", "").strip()
PORTAL_PRIVACY_URL = os.environ.get("BILLING_PRIVACY_URL", f"{FRONTEND_URL}/privacy")
PORTAL_TERMS_URL = os.environ.get("BILLING_TERMS_URL", f"{FRONTEND_URL}/terms")
# Stable marker so we can find a config we previously created instead of
# making a new one every cold start (idempotency without a DB row).
_PORTAL_CONFIG_MARKER = "genly-portal-v1"
_portal_config_id_cache: Optional[str] = None
_portal_config_lock = threading.Lock()

router = APIRouter(prefix="/billing", tags=["billing"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_or_create_stripe_customer(db: Session, user: User) -> str:
    """Ensure the user has a Stripe customer ID."""
    if user.stripe_customer_id:
        return user.stripe_customer_id

    customer = stripe.Customer.create(
        email=user.email or f"{user.username}@genly.ai",
        name=user.username,
        metadata={"user_id": str(user.id), "tenant_id": user.tenant_id},
    )
    user.stripe_customer_id = customer.id
    db.commit()
    return customer.id


def build_portal_configuration_params() -> dict:
    """Explicit portal feature set. Shared by setup_stripe.py and the lazy
    in-app ensure path so both create an identical configuration.

    Plan switching is intentionally OMITTED: all upgrades/downgrades go
    through the in-app /billing/change-plan flow, which enforces the Fase 1
    downgrade guardrail. The hosted portal has no equivalent guardrail, so
    offering subscription_update there would let a user downgrade below
    their month's usage and hit a next-cycle 402. The portal owns card +
    invoice history + cancel-at-period-end; plan changes stay in-app.
    """
    return {
        "business_profile": {
            "headline": "GenLy AI",
            "privacy_policy_url": PORTAL_PRIVACY_URL,
            "terms_of_service_url": PORTAL_TERMS_URL,
        },
        "features": {
            "payment_method_update": {"enabled": True},
            "invoice_history": {"enabled": True},
            "subscription_cancel": {
                "enabled": True,
                "mode": "at_period_end",
                "proration_behavior": "none",
            },
        },
        "metadata": {"genly_marker": _PORTAL_CONFIG_MARKER},
    }


def _ensure_portal_configuration() -> Optional[str]:
    """Return a portal Configuration id, or None to fall back to Stripe's
    default config. Resolution order: pinned env -> in-process cache ->
    existing config found by our marker -> create-once. Never raises — on
    any Stripe error returns None so the caller keeps today's behaviour
    (Session.create with no explicit configuration)."""
    global _portal_config_id_cache
    if STRIPE_PORTAL_CONFIG_ID:
        return STRIPE_PORTAL_CONFIG_ID
    if _portal_config_id_cache:
        return _portal_config_id_cache
    with _portal_config_lock:
        if _portal_config_id_cache:
            return _portal_config_id_cache
        try:
            existing = stripe.billing_portal.Configuration.list(limit=100)
            for cfg in existing.auto_paging_iter():
                meta = dict(getattr(cfg, "metadata", None) or {})
                active = cfg["active"] if isinstance(cfg, dict) else getattr(cfg, "active", True)
                if meta.get("genly_marker") == _PORTAL_CONFIG_MARKER and active:
                    _portal_config_id_cache = cfg["id"] if isinstance(cfg, dict) else cfg.id
                    return _portal_config_id_cache
            cfg = stripe.billing_portal.Configuration.create(
                **build_portal_configuration_params()
            )
            _portal_config_id_cache = cfg["id"] if isinstance(cfg, dict) else cfg.id
            logger.info("Created Stripe portal configuration %s", _portal_config_id_cache)
            return _portal_config_id_cache
        except stripe.error.StripeError as exc:
            logger.error("Could not ensure portal configuration; using default: %s", exc)
            return None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

class CheckoutRequest(BaseModel):
    plan_id: str


@router.post("/checkout")
async def create_checkout_session(
    body: CheckoutRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a Stripe Checkout session for a plan subscription."""
    if not stripe.api_key:
        raise HTTPException(status_code=503, detail="Billing not configured")

    plan = PLANS.get(body.plan_id)
    if not plan or not plan.get("stripe_price_id"):
        raise HTTPException(status_code=400, detail="Invalid plan")

    user = db.query(User).filter(User.id == current_user["id"]).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    customer_id = get_or_create_stripe_customer(db, user)

    try:
        session = stripe.checkout.Session.create(
            customer=customer_id,
            payment_method_types=["card"],
            line_items=[{
                "price": plan["stripe_price_id"],
                "quantity": 1,
            }],
            mode="subscription",
            success_url=f"{FRONTEND_URL}/?billing=success&session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{FRONTEND_URL}/?billing=cancelled",
            metadata={
                "user_id": str(user.id),
                "plan_id": body.plan_id,
            },
            subscription_data={
                "metadata": {
                    "user_id": str(user.id),
                    "plan_id": body.plan_id,
                },
            },
        )
    except stripe.error.StripeError as exc:
        logger.error("Stripe checkout session creation failed for user %s: %s", user.id, exc)
        raise HTTPException(
            status_code=502,
            detail="No se pudo crear la sesión de pago. Intentá de nuevo en unos segundos.",
        )

    return {"checkout_url": session.url, "session_id": session.id}


@router.post("/portal")
async def create_portal_session(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a Stripe Customer Portal session for managing subscription."""
    if not stripe.api_key:
        raise HTTPException(status_code=503, detail="Billing not configured")

    user = db.query(User).filter(User.id == current_user["id"]).first()
    if not user or not user.stripe_customer_id:
        raise HTTPException(status_code=400, detail="No billing account found")

    # Resolve our explicit portal configuration (update card / invoices /
    # cancel-at-period-end). When it can't be resolved we fall back to the
    # account default by omitting the kwarg entirely — Stripe rejects an
    # explicit configuration=None, so build the kwargs conditionally.
    portal_config_id = _ensure_portal_configuration()
    session_kwargs = {
        "customer": user.stripe_customer_id,
        "return_url": f"{FRONTEND_URL}/?view=settings",
    }
    if portal_config_id:
        session_kwargs["configuration"] = portal_config_id

    try:
        session = stripe.billing_portal.Session.create(**session_kwargs)
    except stripe.error.StripeError as exc:
        logger.error("Stripe portal session creation failed for user %s: %s", user.id, exc)
        raise HTTPException(
            status_code=502,
            detail="No se pudo abrir el portal de facturación. Intentá de nuevo en unos segundos.",
        )

    return {"portal_url": session.url}


class ChangePlanRequest(BaseModel):
    plan_id: str


class ChangePlanPreviewRequest(BaseModel):
    plan_id: str


def _assert_downgrade_allowed(db: Session, user: User, target_plan_id: str,
                              target_plan: dict, current_user: dict) -> None:
    """Refuse a mid-cycle downgrade to a plan whose monthly limit is below what
    the user has ALREADY approved this cycle — otherwise the next generation
    402s the instant the webhook flips plan_id, blocking a user who paid for
    the higher tier. Overage accounts (UMG-style) are exempt. Shared by
    /change-plan and /change-plan/preview so the preview can never say OK while
    the real apply 400s. Raises HTTPException(400) when blocked."""
    from auth import get_plan_usage
    usage = get_plan_usage(
        db, user.id, current_user["tenant_id"], current_user.get("plan", "100")
    )
    target_limit = target_plan.get("limit", 0)
    if not user.allow_overage and usage["used"] > target_limit:
        raise HTTPException(
            status_code=400,
            detail=(
                f"No podés cambiar al plan {target_plan_id} este mes: ya aprobaste "
                f"{usage['used']} videos y ese plan permite {target_limit}. "
                f"Podés hacerlo a partir de tu próxima renovación."
            ),
        )


@router.post("/change-plan")
async def change_plan(
    body: ChangePlanRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Change the user's subscription plan (upgrade/downgrade)."""
    if not stripe.api_key:
        raise HTTPException(status_code=503, detail="Billing not configured")

    plan = PLANS.get(body.plan_id)
    if not plan or not plan.get("stripe_price_id"):
        raise HTTPException(status_code=400, detail="Invalid plan")

    user = db.query(User).filter(User.id == current_user["id"]).first()
    if not user or not user.stripe_subscription_id:
        raise HTTPException(status_code=400, detail="No active subscription")

    # Downgrade guardrail (shared with /change-plan/preview so the modal's
    # preview can never say OK while this apply 400s). Refuses a mid-cycle drop
    # below the user's already-approved count; overage accounts are exempt.
    _assert_downgrade_allowed(db, user, body.plan_id, plan, current_user)

    # Get current subscription
    subscription = stripe.Subscription.retrieve(user.stripe_subscription_id)
    current_item_id = subscription["items"]["data"][0].id

    # Idempotency key prevents a retried request (network blip, double-click)
    # from creating duplicate prorations. The local plan_id is intentionally
    # NOT mutated here — the customer.subscription.updated webhook is the
    # authoritative source. This keeps Stripe and our DB consistent even if
    # this call partially succeeds.
    idem_key = f"change-plan:{user.id}:{body.plan_id}:{current_item_id}"
    stripe.Subscription.modify(
        user.stripe_subscription_id,
        items=[{
            "id": current_item_id,
            "price": plan["stripe_price_id"],
        }],
        proration_behavior="create_prorations",
        metadata={"plan_id": body.plan_id, "user_id": str(user.id)},
        idempotency_key=idem_key,
    )

    # Don't optimistically grant the plan — the webhook will commit it once
    # Stripe confirms. Return the request was accepted.
    return {"ok": True, "plan_pending": body.plan_id}


@router.post("/change-plan/preview")
async def change_plan_preview(
    body: ChangePlanPreviewRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Preview the proration for switching to body.plan_id WITHOUT applying it,
    so the confirmation modal can show "charged/credited $X now". Surfaces the
    Fase-1 downgrade guardrail (same 400) so a blocked downgrade reads cleanly
    in the modal instead of only failing at apply time."""
    if not stripe.api_key:
        raise HTTPException(status_code=503, detail="Billing not configured")

    plan = PLANS.get(body.plan_id)
    if not plan or not plan.get("stripe_price_id"):
        raise HTTPException(status_code=400, detail="Invalid plan")

    user = db.query(User).filter(User.id == current_user["id"]).first()
    if not user or not user.stripe_subscription_id:
        raise HTTPException(status_code=400, detail="No active subscription")
    if user.plan_id == body.plan_id:
        raise HTTPException(status_code=400, detail="Ya estás en ese plan")

    # SAME guardrail as /change-plan (one helper → no drift).
    _assert_downgrade_allowed(db, user, body.plan_id, plan, current_user)

    try:
        subscription = stripe.Subscription.retrieve(user.stripe_subscription_id)
        current_item_id = subscription["items"]["data"][0].id
        # Pin the proration date so the line filter below is deterministic.
        proration_date = int(datetime.now(timezone.utc).timestamp())
        upcoming = stripe.Invoice.upcoming(
            customer=user.stripe_customer_id,
            subscription=user.stripe_subscription_id,
            subscription_items=[{
                "id": current_item_id,
                "price": plan["stripe_price_id"],
            }],
            subscription_proration_behavior="create_prorations",
            subscription_proration_date=proration_date,
        )
    except stripe.error.StripeError as exc:
        logger.error("proration preview failed for user %s: %s", user.id, exc)
        raise HTTPException(status_code=502, detail="No se pudo calcular el prorrateo. Intentá de nuevo.")

    # Sum ONLY the proration lines at our pinned date — the rest of `upcoming`
    # is next cycle's full charge, which we must not show as "now". Positive =>
    # charged now; negative => credited to the customer balance.
    proration_cents = 0
    for line in upcoming["lines"]["data"]:
        if line.get("proration") and line.get("period", {}).get("start") == proration_date:
            proration_cents += line.get("amount", 0)

    return {
        "plan_id": body.plan_id,
        "currency": (upcoming.get("currency") or "usd"),
        "proration_cents": proration_cents,
        "amount_due_cents": upcoming.get("amount_due", 0),
        "proration_date": proration_date,
        "immediate": True,
    }


@router.post("/cancel")
async def cancel_subscription(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Schedule cancellation at period end (cancel_at_period_end=true). The user
    keeps access until current_period_end; fully reversible via /reactivate.
    plan_id is NOT touched here — the subscription.deleted webhook flips it to
    free when the period actually ends (source of truth)."""
    if not stripe.api_key:
        raise HTTPException(status_code=503, detail="Billing not configured")
    user = db.query(User).filter(User.id == current_user["id"]).first()
    if not user or not user.stripe_subscription_id:
        raise HTTPException(status_code=400, detail="No active subscription")
    try:
        sub = stripe.Subscription.modify(
            user.stripe_subscription_id,
            cancel_at_period_end=True,
            idempotency_key=f"cancel:{user.id}:{user.stripe_subscription_id}",
        )
    except stripe.error.StripeError as exc:
        logger.error("cancel failed for user %s: %s", user.id, exc)
        raise HTTPException(status_code=502, detail="No se pudo programar la cancelación. Intentá de nuevo.")

    period_end = getattr(sub, "current_period_end", None)
    if user.email:
        access_until = (
            datetime.fromtimestamp(int(period_end), tz=timezone.utc).strftime("%d %b %Y")
            if period_end else ""
        )
        _send_email_async(
            emails.send_cancellation_scheduled,
            email=user.email, username=user.username, access_until=access_until,
        )
    return {"ok": True, "cancel_at_period_end": True, "current_period_end": period_end}


@router.post("/reactivate")
async def reactivate_subscription(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Undo a scheduled cancellation (cancel_at_period_end=false). No new
    checkout — the subscription was never interrupted."""
    if not stripe.api_key:
        raise HTTPException(status_code=503, detail="Billing not configured")
    user = db.query(User).filter(User.id == current_user["id"]).first()
    if not user or not user.stripe_subscription_id:
        raise HTTPException(status_code=400, detail="No active subscription")
    try:
        sub = stripe.Subscription.modify(
            user.stripe_subscription_id,
            cancel_at_period_end=False,
            idempotency_key=f"reactivate:{user.id}:{user.stripe_subscription_id}",
        )
    except stripe.error.StripeError as exc:
        logger.error("reactivate failed for user %s: %s", user.id, exc)
        raise HTTPException(status_code=502, detail="No se pudo reactivar. Intentá de nuevo.")
    return {"ok": True, "cancel_at_period_end": False,
            "current_period_end": getattr(sub, "current_period_end", None)}


@router.get("/invoices")
async def list_invoices(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the user's invoice history."""
    invoices = (
        db.query(Invoice)
        .filter(Invoice.user_id == current_user["id"])
        .order_by(Invoice.created_at.desc())
        .limit(50)
        .all()
    )
    return [inv.to_dict() for inv in invoices]


@router.get("/subscription")
async def get_subscription(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return current subscription details."""
    user = db.query(User).filter(User.id == current_user["id"]).first()

    result = {
        "plan": user.plan_id,
        "plan_details": PLANS.get(user.plan_id, PLANS["free"]),
        "has_subscription": bool(user.stripe_subscription_id),
        "stripe_customer_id": user.stripe_customer_id,
        # Dunning state — lets the Facturación tab render the past-due
        # notice inline (mirrors the global banner driven by /auth/me).
        "billing_status": getattr(user, "billing_status", "active") or "active",
    }

    if user.stripe_subscription_id and stripe.api_key:
        try:
            sub = stripe.Subscription.retrieve(user.stripe_subscription_id)
            result["subscription"] = {
                "status": sub.status,
                "current_period_end": sub.current_period_end,
                "cancel_at_period_end": sub.cancel_at_period_end,
            }
        except stripe.error.StripeError:
            pass

    return result


@router.get("/payment-method")
async def get_payment_method(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the customer's DEFAULT card for read-only display.

    Shape: {"payment_method": {brand,last4,exp_month,exp_year}} or
    {"payment_method": null}. Stripe only ever returns brand/last4/exp — no
    PAN. Degrades to null when Stripe is unconfigured, the user has no
    customer, or no card is on file (mirrors /subscription). Card ENTRY
    happens only in the hosted portal (PCI), never here.
    """
    user = db.query(User).filter(User.id == current_user["id"]).first()
    if not stripe.api_key or not user or not user.stripe_customer_id:
        return {"payment_method": None}

    def _card_dict(card) -> Optional[dict]:
        # `card` is a StripeObject (PaymentMethod.card or a card Source).
        # Guard every field: an unexpanded ref comes back as a bare str id.
        if not card or isinstance(card, str):
            return None
        brand = getattr(card, "brand", None)
        last4 = getattr(card, "last4", None)
        if not (brand and last4):
            return None
        return {
            "brand": brand,
            "last4": last4,
            "exp_month": getattr(card, "exp_month", None),
            "exp_year": getattr(card, "exp_year", None),
        }

    try:
        customer = stripe.Customer.retrieve(
            user.stripe_customer_id,
            expand=["invoice_settings.default_payment_method", "default_source"],
        )
        # (1) invoice_settings.default_payment_method — the canonical default
        inv = getattr(customer, "invoice_settings", None)
        pm = getattr(inv, "default_payment_method", None) if inv else None
        if pm and not isinstance(pm, str):
            card = _card_dict(getattr(pm, "card", None))
            if card:
                return {"payment_method": card}

        # (2) fall back to the subscription's default_payment_method
        if user.stripe_subscription_id:
            sub = stripe.Subscription.retrieve(
                user.stripe_subscription_id,
                expand=["default_payment_method"],
            )
            spm = getattr(sub, "default_payment_method", None)
            if spm and not isinstance(spm, str):
                card = _card_dict(getattr(spm, "card", None))
                if card:
                    return {"payment_method": card}

        # (3) legacy default_source (a card Source carries brand/last4/exp inline)
        src = getattr(customer, "default_source", None)
        if src and not isinstance(src, str):
            card = _card_dict(src)
            if card:
                return {"payment_method": card}
    except stripe.error.StripeError as exc:
        logger.warning("payment-method lookup failed for user %s: %s", user.id, exc)
        return {"payment_method": None}

    return {"payment_method": None}


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------

@router.post("/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    """Handle Stripe webhook events."""
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    if not STRIPE_WEBHOOK_SECRET.strip():
        # Without a signing secret the only safe thing to do is refuse the
        # request. Falling through to json.loads(payload) lets anyone forge
        # checkout/subscription events. An empty string is treated the same
        # as a missing value — a misconfigured empty env var must not bypass
        # signature verification.
        if _REQUIRE_WEBHOOK_SIGNATURE:
            logger.error("Refusing webhook: STRIPE_WEBHOOK_SECRET is not configured")
            raise HTTPException(status_code=503, detail="Webhook signing not configured")
        # No Stripe key at all (local dev with billing disabled): accept and ignore.
        return JSONResponse({"received": False, "reason": "billing_disabled"})

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET,
        )
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        logger.warning(f"Webhook signature verification failed: {e}")
        raise HTTPException(status_code=400, detail="Invalid signature")

    event_type = event.get("type", "")
    data = event.get("data", {}).get("object", {})

    logger.info(f"Stripe webhook: {event_type}")

    if event_type == "checkout.session.completed":
        _handle_checkout_completed(db, data)
    elif event_type in ("customer.subscription.updated", "customer.subscription.created"):
        # `created` is a safety net: if checkout.session.completed delivery
        # fails, the subscription's metadata.plan_id (stamped in
        # subscription_data at checkout) still lets us bind the plan.
        _handle_subscription_updated(db, data)
    elif event_type == "customer.subscription.deleted":
        _handle_subscription_deleted(db, data)
    elif event_type == "invoice.paid":
        _handle_invoice_paid(db, data)
    elif event_type == "invoice.payment_failed":
        _handle_invoice_failed(db, data)

    return JSONResponse({"received": True})


def _find_user_by_customer(db: Session, customer_id: str) -> Optional[User]:
    return db.query(User).filter(User.stripe_customer_id == customer_id).first()


def _handle_checkout_completed(db: Session, data: dict):
    customer_id = data.get("customer")
    subscription_id = data.get("subscription")
    metadata = data.get("metadata", {})
    plan_id = metadata.get("plan_id", "100")

    # Only resolve via stripe_customer_id. metadata.user_id was a fallback
    # path that let a forged event rebind any user's billing identity.
    # The customer is created server-side in get_or_create_stripe_customer
    # and its id is stamped on the user row at that moment, so any legit
    # checkout will already match here.
    user = _find_user_by_customer(db, customer_id)
    if not user:
        logger.warning(
            "checkout.session.completed for unknown stripe_customer_id=%s; ignoring",
            customer_id,
        )
        return

    user.stripe_subscription_id = subscription_id
    if plan_id in PLANS:
        user.plan_id = plan_id
    else:
        logger.warning(
            "checkout.session.completed: unrecognised plan_id=%r for customer=%s; "
            "subscription_id persisted but plan NOT updated",
            plan_id, customer_id,
        )
    db.commit()
    logger.info(f"User {user.username} subscribed to plan {plan_id}")


def _handle_subscription_updated(db: Session, data: dict):
    customer_id = data.get("customer")
    user = _find_user_by_customer(db, customer_id)
    if not user:
        return

    old_plan = user.plan_id  # snapshot BEFORE mutation for the plan-change email
    plan_id = data.get("metadata", {}).get("plan_id")
    if plan_id and plan_id in PLANS:
        user.plan_id = plan_id
    elif plan_id:
        logger.warning(
            "customer.subscription.updated: unrecognised plan_id=%r for customer=%s; "
            "plan NOT updated",
            plan_id, customer_id,
        )

    # Dunning state for the in-app banner. Stripe drives every transition:
    # past_due/unpaid → show "fix your card"; active/trialing → all clear
    # (this is also the recovery path when a Smart Retry finally succeeds).
    # Transient/other statuses (incomplete, paused, canceled) are left
    # untouched so we don't flap the banner on intermediate events.
    sub_status = data.get("status")
    if sub_status in ("past_due", "unpaid"):
        if user.billing_status != "past_due":
            logger.warning("User %s → past_due (subscription.%s)", user.username, sub_status)
        user.billing_status = "past_due"
    elif sub_status in ("active", "trialing"):
        user.billing_status = "active"

    user.stripe_subscription_id = data.get("id")
    db.commit()

    # Plan-change confirmation: only on a real transition between two PAID
    # plans. free → X is the initial subscribe (covered by the welcome email),
    # so skip it here to avoid a duplicate/contradictory message.
    if (user.email and plan_id and plan_id in PLANS
            and plan_id != old_plan and old_plan not in (None, "free")):
        old_limit = PLANS.get(old_plan, {}).get("limit", 0)
        new_limit = PLANS[plan_id].get("limit", 0)
        direction = ("upgrade" if new_limit > old_limit
                     else "downgrade" if new_limit < old_limit else "changed")
        _send_email_async(
            emails.send_plan_changed,
            email=user.email,
            username=user.username,
            old_plan=old_plan,
            new_plan=plan_id,
            new_limit=new_limit,
            direction=direction,
        )


def _handle_subscription_deleted(db: Session, data: dict):
    customer_id = data.get("customer")
    user = _find_user_by_customer(db, customer_id)
    if not user:
        return

    # Capture before we mutate/lose context (for the cancellation email).
    email = user.email
    username = user.username
    access_until = ""
    period_end = data.get("current_period_end")
    if period_end:
        try:
            access_until = datetime.fromtimestamp(
                int(period_end), tz=timezone.utc
            ).strftime("%d %b %Y")
        except (TypeError, ValueError, OverflowError):
            access_until = ""

    user.plan_id = "free"
    user.stripe_subscription_id = None
    # No subscription left to be past-due on — clear the dunning banner so a
    # cancelled (or grace-period-exhausted) user lands cleanly on free.
    user.billing_status = "active"
    db.commit()
    logger.info(f"User {username} subscription cancelled → free plan")

    if email:
        _send_email_async(
            emails.send_subscription_cancelled,
            email=email,
            username=username,
            access_until=access_until,
        )


def _handle_invoice_paid(db: Session, data: dict):
    from sqlalchemy.exc import IntegrityError

    customer_id = data.get("customer")
    user = _find_user_by_customer(db, customer_id)
    if not user:
        return

    # A successful charge clears any prior dunning state — set it before the
    # duplicate short-circuit so a retried invoice.paid delivery still heals
    # the banner. (db.commit() below persists it in both code paths.)
    if user.billing_status != "active":
        logger.info("User %s payment recovered → billing_status=active", user.username)
        user.billing_status = "active"

    # Avoid duplicates
    stripe_inv_id = data.get("id")
    existing = db.query(Invoice).filter(Invoice.stripe_invoice_id == stripe_inv_id).first()
    if existing:
        existing.status = "paid"
        db.commit()
        return

    period_start = data.get("period_start")
    period_end = data.get("period_end")

    invoice = Invoice(
        user_id=user.id,
        stripe_invoice_id=stripe_inv_id,
        amount_cents=data.get("amount_paid", 0),
        currency=data.get("currency", "usd"),
        status="paid",
        description=f"GenLy AI — Plan {user.plan_id}",
        invoice_url=data.get("hosted_invoice_url"),
        invoice_pdf=data.get("invoice_pdf"),
        period_start=datetime.fromtimestamp(period_start, tz=timezone.utc) if period_start else None,
        period_end=datetime.fromtimestamp(period_end, tz=timezone.utc) if period_end else None,
    )
    db.add(invoice)
    try:
        db.commit()
    except IntegrityError:
        # Concurrent webhook delivery (Stripe retries on 5xx) inserted the
        # same row in parallel. The unique constraint on stripe_invoice_id
        # caught it — treat as already-applied so Stripe doesn't keep
        # retrying us.
        db.rollback()
        logger.info("invoice.paid duplicate for %s — already recorded", stripe_inv_id)
        return

    if user.email:
        amount_paid = data.get("amount_paid", 0) / 100
        currency = data.get("currency", "usd")
        invoice_url = data.get("hosted_invoice_url", "")
        _send_email_async(
            emails.send_invoice_paid,
            email=user.email,
            username=user.username,
            amount=amount_paid,
            currency=currency,
            invoice_url=invoice_url,
        )

        # One-time subscription welcome: ONLY the first invoice of a new sub.
        # Recurring monthly invoices have billing_reason='subscription_cycle'
        # (and proration invoices 'subscription_update') → receipt only, never
        # a second welcome. The duplicate-invoice short-circuit above means
        # Stripe retries can't re-send it either.
        if data.get("billing_reason") == "subscription_create":
            _plan = PLANS.get(user.plan_id, {})
            _send_email_async(
                emails.send_subscription_welcome,
                email=user.email,
                username=user.username,
                plan=user.plan_id,
                limit=_plan.get("limit", 0),
                monthly_price_usd=(_plan.get("monthly_price", 0) or 0),
            )


def _handle_invoice_failed(db: Session, data: dict):
    customer_id = data.get("customer")
    user = _find_user_by_customer(db, customer_id)
    if not user:
        return

    # invoice.payment_failed is the earliest, most reliable dunning signal
    # (it fires before the subscription itself flips to past_due). Flag the
    # user so the in-app banner shows even if the subscription.updated event
    # is delayed or dropped.
    user.billing_status = "past_due"

    stripe_inv_id = data.get("id")
    existing = db.query(Invoice).filter(Invoice.stripe_invoice_id == stripe_inv_id).first()
    if existing:
        existing.status = "failed"
    else:
        invoice = Invoice(
            user_id=user.id,
            stripe_invoice_id=stripe_inv_id,
            amount_cents=data.get("amount_due", 0),
            currency=data.get("currency", "usd"),
            status="failed",
            description=f"Payment failed — Plan {user.plan_id}",
        )
        db.add(invoice)
    db.commit()
    logger.warning(f"Payment failed for user {user.username}")

    if user.email:
        amount_due = data.get("amount_due", 0) / 100
        currency = data.get("currency", "usd")
        # Stripe's next scheduled retry (Smart Retries) — surfacing it turns the
        # alert into a calm dunning message ("we'll retry on X; fix your card").
        retry_date = ""
        next_attempt = data.get("next_payment_attempt")
        if next_attempt:
            from datetime import datetime, timezone
            retry_date = datetime.fromtimestamp(
                int(next_attempt), tz=timezone.utc
            ).strftime("%d %b %Y")
        _send_email_async(
            emails.send_payment_failed,
            email=user.email,
            username=user.username,
            amount=amount_due,
            currency=currency,
            retry_date=retry_date,
        )
