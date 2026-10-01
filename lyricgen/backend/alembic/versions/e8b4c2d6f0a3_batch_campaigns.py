"""durable batch campaigns, items and isolated workload routing

Revision ID: e8b4c2d6f0a3
Revises: d7c2f9a41b83
Create Date: 2026-08-31 18:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "e8b4c2d6f0a3"
down_revision: Union[str, Sequence[str], None] = "d7c2f9a41b83"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _json():
    return postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "batch_campaigns",
        sa.Column("id", sa.String(length=12), nullable=False),
        sa.Column("tenant_id", sa.String(length=100), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="active", nullable=False),
        sa.Column("expected_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("default_render_params", _json(), server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_batch_campaigns_tenant_id", "batch_campaigns", ["tenant_id"])
    op.create_index("ix_batch_campaigns_created_by", "batch_campaigns", ["created_by"])
    op.create_index(
        "ix_batch_campaigns_tenant_created", "batch_campaigns",
        ["tenant_id", sa.text("created_at DESC")],
    )

    op.create_table(
        "batch_campaign_items",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("campaign_id", sa.String(length=12), nullable=False),
        sa.Column("tenant_id", sa.String(length=100), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=500), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=True),
        sa.Column("artist", sa.String(length=255), nullable=True),
        sa.Column("technical_code", sa.String(length=64), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("metadata_error", sa.String(length=255), nullable=True),
        sa.Column("upload_state", sa.String(length=20), server_default="registered", nullable=False),
        sa.Column("upload_key", sa.Text(), nullable=True),
        sa.Column("multipart_upload_id", sa.Text(), nullable=True),
        sa.Column("upload_error", sa.String(length=500), nullable=True),
        sa.Column("upload_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("render_overrides", _json(), server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["batch_campaigns.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("campaign_id", "sha256", name="uq_batch_item_campaign_sha"),
        sa.UniqueConstraint("campaign_id", "technical_code", name="uq_batch_item_campaign_code"),
    )
    op.create_index("ix_batch_campaign_items_campaign_id", "batch_campaign_items", ["campaign_id"])
    op.create_index("ix_batch_campaign_items_tenant_id", "batch_campaign_items", ["tenant_id"])
    op.create_index(
        "ix_batch_items_campaign_upload", "batch_campaign_items",
        ["campaign_id", "upload_state", "ordinal"],
    )

    op.create_table(
        "batch_upload_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("campaign_id", sa.String(length=12), nullable=False),
        sa.Column("tenant_id", sa.String(length=100), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=True),
        sa.Column("code_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["batch_campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code_hash"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_batch_upload_sessions_campaign_id", "batch_upload_sessions", ["campaign_id"])
    op.create_index("ix_batch_upload_sessions_tenant_id", "batch_upload_sessions", ["tenant_id"])
    op.create_index("ix_batch_upload_sessions_code_hash", "batch_upload_sessions", ["code_hash"])
    op.create_index("ix_batch_upload_sessions_token_hash", "batch_upload_sessions", ["token_hash"])

    # batch_alter_table is a normal ALTER on PostgreSQL and a safe table-copy
    # on SQLite, which cannot add foreign keys after CREATE TABLE.
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.add_column(sa.Column(
            "workload_class", sa.String(length=16),
            server_default="interactive", nullable=False,
        ))
        batch_op.add_column(sa.Column("campaign_id", sa.String(length=12), nullable=True))
        batch_op.add_column(sa.Column("campaign_item_id", sa.String(length=36), nullable=True))
        batch_op.create_foreign_key(
            "fk_jobs_campaign", "batch_campaigns", ["campaign_id"], ["id"],
        )
        batch_op.create_foreign_key(
            "fk_jobs_campaign_item", "batch_campaign_items", ["campaign_item_id"], ["id"],
        )
        batch_op.create_index("ix_jobs_workload_class", ["workload_class"])
        batch_op.create_index("ix_jobs_campaign_id", ["campaign_id"])
        batch_op.create_index("ix_jobs_campaign_item_id", ["campaign_item_id"], unique=True)
    op.add_column("editor_documents", sa.Column("lock_session_id", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("editor_documents", "lock_session_id")
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.drop_index("ix_jobs_campaign_item_id")
        batch_op.drop_index("ix_jobs_campaign_id")
        batch_op.drop_index("ix_jobs_workload_class")
        batch_op.drop_constraint("fk_jobs_campaign_item", type_="foreignkey")
        batch_op.drop_constraint("fk_jobs_campaign", type_="foreignkey")
        batch_op.drop_column("campaign_item_id")
        batch_op.drop_column("campaign_id")
        batch_op.drop_column("workload_class")
    op.drop_index("ix_batch_upload_sessions_token_hash", table_name="batch_upload_sessions")
    op.drop_index("ix_batch_upload_sessions_code_hash", table_name="batch_upload_sessions")
    op.drop_index("ix_batch_upload_sessions_tenant_id", table_name="batch_upload_sessions")
    op.drop_index("ix_batch_upload_sessions_campaign_id", table_name="batch_upload_sessions")
    op.drop_table("batch_upload_sessions")
    op.drop_index("ix_batch_items_campaign_upload", table_name="batch_campaign_items")
    op.drop_index("ix_batch_campaign_items_tenant_id", table_name="batch_campaign_items")
    op.drop_index("ix_batch_campaign_items_campaign_id", table_name="batch_campaign_items")
    op.drop_table("batch_campaign_items")
    op.drop_index("ix_batch_campaigns_tenant_created", table_name="batch_campaigns")
    op.drop_index("ix_batch_campaigns_created_by", table_name="batch_campaigns")
    op.drop_index("ix_batch_campaigns_tenant_id", table_name="batch_campaigns")
    op.drop_table("batch_campaigns")
