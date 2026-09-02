"""add billing_status to users

Revision ID: c4d8e1f6a2b9
Revises: b7d4e3a2c109
Create Date: 2026-06-02 10:00:00.000000

Dunning state for the in-app "payment failed" banner (Fase 1.5).

Before this column, a failed charge only logged + emailed; the user kept
paid access indefinitely and the app showed nothing. After it, the
Stripe webhooks (invoice.payment_failed / subscription.updated:past_due)
flip the row to "past_due"; a recovered charge (invoice.paid or
subscription.updated:active) flips it back to "active". The frontend
reads it from /auth/me to show a global "actualizá tu tarjeta" banner.

NOT NULL with server_default "active": every existing row is treated as
in-good-standing on deploy (the safe default — no spurious banners), and
new rows start "active" without the app having to set it.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "c4d8e1f6a2b9"
down_revision: Union[str, Sequence[str], None] = "b7d4e3a2c109"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "billing_status",
            sa.String(length=20),
            nullable=False,
            server_default="active",
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "billing_status")
