"""durable editor drafts, versions, collaboration locks and product events

Revision ID: c4f2a7e1d9b0
Revises: e5f6a7b8c9d0
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "c4f2a7e1d9b0"
down_revision: Union[str, Sequence[str], None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "editor_documents",
        sa.Column("job_id", sa.String(length=12), nullable=False),
        sa.Column("tenant_id", sa.String(length=100), nullable=False),
        sa.Column("current_segments", postgresql.JSONB(), nullable=False),
        sa.Column("original_segments", postgresql.JSONB(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lock_user_id", sa.Integer(), nullable=True),
        sa.Column("lock_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.job_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["lock_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("job_id"),
    )
    op.create_index("ix_editor_documents_tenant_id", "editor_documents", ["tenant_id"])

    op.create_table(
        "editor_versions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=12), nullable=False),
        sa.Column("tenant_id", sa.String(length=100), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("segments", postgresql.JSONB(), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.String(length=20), nullable=False),
        sa.Column("is_approved", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.job_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_editor_versions_job_id", "editor_versions", ["job_id"])
    op.create_index("ix_editor_versions_tenant_id", "editor_versions", ["tenant_id"])
    op.create_index("ix_editor_versions_job_revision", "editor_versions", ["job_id", "revision"], unique=True)
    op.create_index("ix_editor_versions_job_created", "editor_versions", ["job_id", "created_at"])

    op.create_table(
        "product_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=100), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("job_id", sa.String(length=12), nullable=True),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("properties", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_product_events_tenant_id", "product_events", ["tenant_id"])
    op.create_index("ix_product_events_job_id", "product_events", ["job_id"])
    op.create_index("ix_product_events_name", "product_events", ["name"])
    op.create_index("ix_product_events_tenant_created", "product_events", ["tenant_id", "created_at"])
    op.create_index("ix_product_events_name_created", "product_events", ["name", "created_at"])


def downgrade() -> None:
    op.drop_table("product_events")
    op.drop_table("editor_versions")
    op.drop_table("editor_documents")
