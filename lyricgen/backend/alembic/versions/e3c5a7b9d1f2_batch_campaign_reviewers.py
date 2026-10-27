"""Per-user access to one campaign's review workspace.

Revision ID: e3c5a7b9d1f2
Revises: d2e4f6a8b0c1
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e3c5a7b9d1f2"
down_revision: Union[str, Sequence[str], None] = "d2e4f6a8b0c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("batch_campaign_reviewers"):
        return
    op.create_table(
        "batch_campaign_reviewers",
        sa.Column("campaign_id", sa.String(length=12), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("granted_by", sa.Integer(), nullable=True),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["campaign_id"], ["batch_campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["granted_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("campaign_id", "user_id"),
    )
    op.create_index(
        "ix_batch_campaign_reviewers_user", "batch_campaign_reviewers",
        ["user_id", "revoked_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_batch_campaign_reviewers_user", table_name="batch_campaign_reviewers")
    op.drop_table("batch_campaign_reviewers")
