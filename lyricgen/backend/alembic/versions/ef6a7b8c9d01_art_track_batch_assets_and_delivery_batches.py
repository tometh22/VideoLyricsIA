"""art-track campaign assets, associations and durable bulk deliveries

Revision ID: ef6a7b8c9d01
Revises: e8b4c2d6f0a3
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "ef6a7b8c9d01"
down_revision: Union[str, Sequence[str], None] = "e8b4c2d6f0a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _json():
    return postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")


def upgrade() -> None:
    op.add_column("batch_campaigns", sa.Column(
        "kind", sa.String(length=24), server_default="lyric_video", nullable=False,
    ))
    op.add_column("batch_campaigns", sa.Column("destination_portal", sa.String(length=32), nullable=True))
    op.add_column("batch_campaigns", sa.Column(
        "preset_version", sa.String(length=40), server_default="art-track-v1", nullable=False,
    ))

    op.create_table(
        "batch_campaign_assets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("campaign_id", sa.String(length=12), nullable=False),
        sa.Column("tenant_id", sa.String(length=100), nullable=False),
        sa.Column("role", sa.String(length=12), nullable=False),
        sa.Column("filename", sa.String(length=500), nullable=False),
        sa.Column("relative_path", sa.String(length=1000), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("mime_type", sa.String(length=120), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("upload_state", sa.String(length=20), server_default="registered", nullable=False),
        sa.Column("upload_key", sa.Text(), nullable=True),
        sa.Column("multipart_upload_id", sa.Text(), nullable=True),
        sa.Column("upload_error", sa.String(length=500), nullable=True),
        sa.Column("upload_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["batch_campaigns.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("campaign_id", "role", "sha256", name="uq_batch_asset_campaign_role_sha"),
    )
    op.create_index("ix_batch_campaign_assets_campaign_id", "batch_campaign_assets", ["campaign_id"])
    op.create_index("ix_batch_campaign_assets_tenant_id", "batch_campaign_assets", ["tenant_id"])
    op.create_index(
        "ix_batch_assets_campaign_role_state", "batch_campaign_assets",
        ["campaign_id", "role", "upload_state"],
    )

    with op.batch_alter_table("batch_campaign_items") as batch_op:
        batch_op.add_column(sa.Column("cover_asset_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("cover_match_state", sa.String(length=20), server_default="pending", nullable=False))
        batch_op.add_column(sa.Column("cover_match_method", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("cover_match_error", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("association_confirmed", sa.Boolean(), server_default="false", nullable=False))
        batch_op.add_column(sa.Column("approved_render_fingerprint", sa.String(length=64), nullable=True))
        batch_op.create_foreign_key(
            "fk_batch_campaign_items_cover_asset", "batch_campaign_assets",
            ["cover_asset_id"], ["id"],
        )
        batch_op.create_index("ix_batch_campaign_items_cover_asset_id", ["cover_asset_id"])

    op.create_table(
        "delivery_batches",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("campaign_id", sa.String(length=12), nullable=False),
        sa.Column("tenant_id", sa.String(length=100), nullable=False),
        sa.Column("destination_portal", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="queued", nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("total_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("sent_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["campaign_id"], ["batch_campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("campaign_id", "idempotency_key", name="uq_delivery_batch_campaign_idempotency"),
    )
    op.create_index("ix_delivery_batches_campaign_id", "delivery_batches", ["campaign_id"])
    op.create_index("ix_delivery_batches_tenant_id", "delivery_batches", ["tenant_id"])
    op.create_index("ix_delivery_batches_campaign_status", "delivery_batches", ["campaign_id", "status"])

    op.create_table(
        "delivery_batch_items",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("delivery_batch_id", sa.String(length=36), nullable=False),
        sa.Column("campaign_id", sa.String(length=12), nullable=False),
        sa.Column("job_id", sa.String(length=12), nullable=False),
        sa.Column("approved_render_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="pending", nullable=False),
        sa.Column("delivery_id", sa.Integer(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_code", sa.String(length=120), nullable=True),
        sa.Column("error_detail", sa.String(length=500), nullable=True),
        sa.Column("receipt", _json(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["delivery_batch_id"], ["delivery_batches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["campaign_id"], ["batch_campaigns.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("delivery_batch_id", "job_id", name="uq_delivery_batch_item_job"),
    )
    op.create_index("ix_delivery_batch_items_delivery_batch_id", "delivery_batch_items", ["delivery_batch_id"])
    op.create_index("ix_delivery_batch_items_campaign_id", "delivery_batch_items", ["campaign_id"])
    op.create_index("ix_delivery_batch_items_job_id", "delivery_batch_items", ["job_id"])
    op.create_index("ix_delivery_batch_items_status", "delivery_batch_items", ["delivery_batch_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_delivery_batch_items_status", table_name="delivery_batch_items")
    op.drop_index("ix_delivery_batch_items_job_id", table_name="delivery_batch_items")
    op.drop_index("ix_delivery_batch_items_campaign_id", table_name="delivery_batch_items")
    op.drop_index("ix_delivery_batch_items_delivery_batch_id", table_name="delivery_batch_items")
    op.drop_table("delivery_batch_items")
    op.drop_index("ix_delivery_batches_campaign_status", table_name="delivery_batches")
    op.drop_index("ix_delivery_batches_tenant_id", table_name="delivery_batches")
    op.drop_index("ix_delivery_batches_campaign_id", table_name="delivery_batches")
    op.drop_table("delivery_batches")
    with op.batch_alter_table("batch_campaign_items") as batch_op:
        batch_op.drop_index("ix_batch_campaign_items_cover_asset_id")
        batch_op.drop_constraint("fk_batch_campaign_items_cover_asset", type_="foreignkey")
        for column in ("approved_render_fingerprint", "association_confirmed", "cover_match_error", "cover_match_method", "cover_match_state", "cover_asset_id"):
            batch_op.drop_column(column)
    op.drop_index("ix_batch_assets_campaign_role_state", table_name="batch_campaign_assets")
    op.drop_index("ix_batch_campaign_assets_tenant_id", table_name="batch_campaign_assets")
    op.drop_index("ix_batch_campaign_assets_campaign_id", table_name="batch_campaign_assets")
    op.drop_table("batch_campaign_assets")
    op.drop_column("batch_campaigns", "preset_version")
    op.drop_column("batch_campaigns", "destination_portal")
    op.drop_column("batch_campaigns", "kind")
