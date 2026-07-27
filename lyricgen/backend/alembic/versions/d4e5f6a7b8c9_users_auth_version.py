"""users.auth_version for deterministic access-token revocation

Revision ID: d4e5f6a7b8c9
Revises: c7a1d9e02f3b
Create Date: 2026-07-22 00:00:00.000000

The column is additive and receives a server-side zero default, so existing
users and legacy access tokens remain valid until an operator or password
flow intentionally increments the user's version.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, Sequence[str], None] = "c7a1d9e02f3b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "auth_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "auth_version")
