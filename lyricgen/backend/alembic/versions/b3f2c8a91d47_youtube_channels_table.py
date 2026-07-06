"""youtube_channels: per-tenant OAuth channel connections

Revision ID: b3f2c8a91d47
Revises: 8802e2187632
Create Date: 2026-07-06 18:00:00.000000

Self-service YouTube channel connections. The OAuth token blob is
Fernet-encrypted at rest (token_crypto.py); the partial unique index
guarantees at most one default channel per tenant.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3f2c8a91d47"
down_revision: Union[str, Sequence[str], None] = "8802e2187632"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "youtube_channels",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=100), nullable=False),
        sa.Column("channel_id", sa.String(length=64), nullable=False),
        sa.Column("channel_title", sa.String(length=255), nullable=True),
        sa.Column("thumbnail_url", sa.String(length=500), nullable=True),
        sa.Column("token_encrypted", sa.Text(), nullable=True),
        sa.Column("scopes", sa.JSON(), nullable=True),
        sa.Column("connected_by", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_refresh_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_refresh_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["connected_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "channel_id", name="uq_yt_channel_per_tenant"),
    )
    op.create_index("ix_youtube_channels_tenant_id", "youtube_channels", ["tenant_id"])
    op.create_index(
        "uq_yt_default_per_tenant",
        "youtube_channels",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("is_default"),
        sqlite_where=sa.text("is_default"),
    )


def downgrade() -> None:
    op.drop_index("uq_yt_default_per_tenant", table_name="youtube_channels")
    op.drop_index("ix_youtube_channels_tenant_id", table_name="youtube_channels")
    op.drop_table("youtube_channels")
