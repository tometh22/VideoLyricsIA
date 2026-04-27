#!/usr/bin/env python3
"""
Create Stripe products and prices for GenLy AI plans.

Usage:
    STRIPE_SECRET_KEY=sk_test_xxx python setup_stripe.py

Prints the price IDs to copy into your .env file.
"""

import os
import sys

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

import stripe

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")

if not stripe.api_key:
    print("ERROR: STRIPE_SECRET_KEY not set.")
    print("Usage: STRIPE_SECRET_KEY=sk_test_xxx python setup_stripe.py")
    sys.exit(1)

PLANS = [
    {"id": "100", "name": "GenLy AI — Plan 100", "videos": 100, "price_usd": 900},
    {"id": "250", "name": "GenLy AI — Plan 250", "videos": 250, "price_usd": 2000},
    {"id": "500", "name": "GenLy AI — Plan 500", "videos": 500, "price_usd": 3500},
    {"id": "1000", "name": "GenLy AI — Plan 1000", "videos": 1000, "price_usd": 6000},
]


def main():
    print("=" * 60)
    print("GenLy AI — Stripe Products Setup")
    print("=" * 60)
    print()

    env_lines = []

    for plan in PLANS:
        print(f"Creating product: {plan['name']}...")

        # Create product
        product = stripe.Product.create(
            name=plan["name"],
            description=f"{plan['videos']} lyric videos per month",
            metadata={"plan_id": plan["id"], "videos": str(plan["videos"])},
        )

        # Create monthly recurring price
        price = stripe.Price.create(
            product=product.id,
            unit_amount=plan["price_usd"] * 100,  # cents
            currency="usd",
            recurring={"interval": "month"},
            metadata={"plan_id": plan["id"]},
        )

        env_key = f"STRIPE_PRICE_{plan['id']}"
        env_lines.append(f"{env_key}={price.id}")
        print(f"  Product: {product.id}")
        print(f"  Price:   {price.id} (${plan['price_usd']}/month)")
        print()

    print("=" * 60)
    print("Add these to your .env file:")
    print("=" * 60)
    for line in env_lines:
        print(line)
    print()

    # Webhook info
    print("=" * 60)
    print("Webhook setup:")
    print("=" * 60)
    print("1. Go to: https://dashboard.stripe.com/webhooks")
    print("2. Add endpoint: https://your-domain.com/billing/webhook")
    print("3. Select events:")
    print("   - checkout.session.completed")
    print("   - customer.subscription.updated")
    print("   - customer.subscription.deleted")
    print("   - invoice.paid")
    print("   - invoice.payment_failed")
    print("4. Copy the webhook signing secret to STRIPE_WEBHOOK_SECRET in .env")


if __name__ == "__main__":
    main()
