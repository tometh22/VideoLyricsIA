"""Bind UMG publication, approval and change verification to exact revisions.

The deliveries tables may be shared between staging and production. Adding
columns idempotently allows the shared portal DB to be upgraded first.

Revision ID: c2e4f6a8b0d1
Revises: e6a8c0d2f4b6, a8c1e4f7b2d9, f8c9d0e1a2b3
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "c2e4f6a8b0d1"
down_revision: Union[str, Sequence[str], None] = (
    "e6a8c0d2f4b6",
    "a8c1e4f7b2d9",
    "f8c9d0e1a2b3",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_COLUMNS = (
    ("jobs", "approved_render_fingerprint", sa.String(64), "VARCHAR(64)"),
    ("jobs", "approved_video_etag", sa.String(128), "VARCHAR(128)"),
    ("deliveries", "approved_revision", sa.Integer(), "INTEGER"),
    ("deliveries", "published_file_keys", sa.JSON(), "JSONB"),
    ("deliveries", "published_file_etags", sa.JSON(), "JSONB"),
    ("deliveries", "published_manifest_hash", sa.String(64), "VARCHAR(64)"),
    ("deliveries", "retired_file_keys", sa.JSON(), "JSONB"),
    ("delivery_change_requests", "requested_revision", sa.Integer(), "INTEGER"),
    ("delivery_change_requests", "verified_render_fingerprint", sa.String(64), "VARCHAR(64)"),
    ("delivery_change_requests", "verified_at", sa.DateTime(timezone=True), "TIMESTAMPTZ"),
    ("delivery_change_requests", "verified_by_user_id", sa.Integer(), "INTEGER"),
    ("delivery_change_requests", "verification_evidence", sa.JSON(), "JSONB"),
)


def upgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        for table, column, _type, sql_type in _COLUMNS:
            op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {sql_type}")
    else:
        inspector = sa.inspect(op.get_bind())
        for table, column, column_type, _sql_type in _COLUMNS:
            existing = {row["name"] for row in inspector.get_columns(table)}
            if column not in existing:
                op.add_column(table, sa.Column(column, column_type, nullable=True))
    inspector = sa.inspect(op.get_bind())
    if "delivery_publish_operations" not in inspector.get_table_names():
        op.create_table(
            "delivery_publish_operations",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("job_id", sa.String(12), nullable=False),
            sa.Column("portal_id", sa.String(20), nullable=False),
            sa.Column("label", sa.String(120), nullable=True),
            sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("expected_fingerprint", sa.String(64), nullable=False),
            sa.Column("status", sa.String(24), nullable=False),
            sa.Column("result", sa.JSON(), nullable=True),
            sa.Column("error", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
    existing_indexes = {
        row["name"] for row in sa.inspect(op.get_bind()).get_indexes("delivery_publish_operations")
    }
    if "ix_delivery_publish_job_portal" not in existing_indexes:
        op.create_index("ix_delivery_publish_job_portal", "delivery_publish_operations", ["job_id", "portal_id", "created_at"])
    if "ix_delivery_publish_status" not in existing_indexes:
        op.create_index("ix_delivery_publish_status", "delivery_publish_operations", ["status", "created_at"])


def downgrade() -> None:
    op.drop_table("delivery_publish_operations")
    for table, column, _type, _sql_type in reversed(_COLUMNS):
        op.drop_column(table, column)
