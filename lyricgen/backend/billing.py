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

# --- Stripe config ---
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:5173")

# Refuse to accept unsigned webhook payloads when billing is wired up: an
# unverified handler lets anyone POST a forged subscription update and grant
# themselves any plan (or hijack another user's billing identity via
# metadata.user_id).
_REQUIRE_WEBHOOK_SIGNATURE = bool(stripe.api_key)

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

    session = stripe.billing_portal.Session.create(
        customer=user.stripe_customer_id,
        return_url=f"{FRONTEND_URL}/?view=settings",
    )

    return {"portal_url": session.url}


class ChangePlanRequest(BaseModel):
    plan_id: str


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


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------

@router.post("/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    """Handle Stripe webhook events."""
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    if not STRIPE_WEBHOOK_SECRET:
        # Without a signing secret the only safe thing to do is refuse the
        # request. Falling through to json.loads(payload) lets anyone forge
        # checkout/subscription events.
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
    elif event_type == "customer.subscription.updated":
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
    db.commit()
    logger.info(f"User {user.username} subscribed to plan {plan_id}")


def _handle_subscription_updated(db: Session, data: dict):
    customer_id = data.get("customer")
    user = _find_user_by_customer(db, customer_id)
    if not user:
        return

    plan_id = data.get("metadata", {}).get("plan_id")
    if plan_id and plan_id in PLANS:
        user.plan_id = plan_id

    user.stripe_subscription_id = data.get("id")
    db.commit()


def _handle_subscription_deleted(db: Session, data: dict):
    customer_id = data.get("customer")
    user = _find_user_by_customer(db, customer_id)
    if not user:
        return

    user.plan_id = "free"
    user.stripe_subscription_id = None
    db.commit()
    logger.info(f"User {user.username} subscription cancelled → free plan")


def _handle_invoice_paid(db: Session, data: dict):
    from sqlalchemy.exc import IntegrityError

    customer_id = data.get("customer")
    user = _find_user_by_customer(db, customer_id)
    if not user:
        return

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
        threading.Thread(
            target=emails.send_invoice_paid,
            kwargs={
                "email": user.email,
                "username": user.username,
                "amount": amount_paid,
                "currency": currency,
                "invoice_url": invoice_url,
            },
            daemon=True,
        ).start()


def _handle_invoice_failed(db: Session, data: dict):
    customer_id = data.get("customer")
    user = _find_user_by_customer(db, customer_id)
    if not user:
        return

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
        threading.Thread(
            target=emails.send_payment_failed,
            kwargs={
                "email": user.email,
                "username": user.username,
                "amount": amount_due,
                "currency": currency,
            },
            daemon=True,
        ).start()
