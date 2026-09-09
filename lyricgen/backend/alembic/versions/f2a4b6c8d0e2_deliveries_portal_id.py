"""Store the destination portal on each delivery row.

Revision ID: f2a4b6c8d0e2
Revises: a1c3e5f7b902
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "f2a4b6c8d0e2"
down_revision: Union[str, Sequence[str], None] = "a1c3e5f7b902"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "deliveries",
        sa.Column(
            "portal_id",
            sa.String(length=20),
            nullable=False,
            server_default="argentina",
        ),
    )
    op.create_index("ix_deliveries_portal_id", "deliveries", ["portal_id"])


def downgrade() -> None:
    op.drop_index("ix_deliveries_portal_id", table_name="deliveries")
    op.drop_column("deliveries", "portal_id")
