"""veo_budget_ledger survives deletable job cleanup

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "veo_budget_ledger",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("scope_hash", sa.String(length=64), nullable=False),
        sa.Column("source_provenance_id", sa.Integer(), nullable=False),
        sa.Column("provider_call_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_provenance_id",
            name="uq_veo_budget_ledger_source_provenance_id",
        ),
    )
    op.create_index(
        "ix_veo_budget_ledger_scope_call_at",
        "veo_budget_ledger",
        ["scope_hash", "provider_call_at"],
    )
    op.create_index(
        "ix_veo_budget_ledger_archived_at",
        "veo_budget_ledger",
        ["archived_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_veo_budget_ledger_archived_at", table_name="veo_budget_ledger",
    )
    op.drop_index(
        "ix_veo_budget_ledger_scope_call_at", table_name="veo_budget_ledger",
    )
    op.drop_table("veo_budget_ledger")
