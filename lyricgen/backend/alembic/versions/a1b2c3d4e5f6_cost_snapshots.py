"""cost_snapshots: monthly invoiced cost per provider

Backs /admin/cost/real and /admin/cost/reconcile. Provider billing APIs
only expose a rolling window, so a month that is never snapshotted is
unrecoverable — this table is the durable record.

Revision ID: a1b2c3d4e5f6
Revises: c4f2a7e1d9b0
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "c4f2a7e1d9b0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "cost_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("period", sa.String(length=7), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        # Nullable so "not configured yet" stays distinguishable from $0.
        sa.Column("amount_usd", sa.Float(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False,
                  server_default="ok"),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("is_estimate", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("breakdown", postgresql.JSONB(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("period", "source",
                            name="uq_cost_snapshot_period_source"),
    )
    op.create_index("ix_cost_snapshots_period", "cost_snapshots", ["period"])
    op.create_index("ix_cost_snapshots_fetched_at", "cost_snapshots",
                    ["fetched_at"])


def downgrade() -> None:
    op.drop_index("ix_cost_snapshots_fetched_at", table_name="cost_snapshots")
    op.drop_index("ix_cost_snapshots_period", table_name="cost_snapshots")
    op.drop_table("cost_snapshots")
