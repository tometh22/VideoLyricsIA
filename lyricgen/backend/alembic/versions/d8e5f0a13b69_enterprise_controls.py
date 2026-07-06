"""enterprise controls: maker-checker, tenant settings, YouTube quota

Revision ID: d8e5f0a13b69
Revises: c7d4e9f02a58
Create Date: 2026-07-06 21:00:00.000000

- users.can_approve_public (maker-checker capability flag)
- tenant_settings (keyed-JSON tenant config; require_public_approval)
- youtube_api_quota (daily unit counter, Pacific-midnight reset)
- publish_jobs: approval + quota-deferral + content-id columns
- uq_publish_active recreated to include pending_approval
- audit_log composite indexes for the filtered admin view
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d8e5f0a13b69"
down_revision: Union[str, Sequence[str], None] = "c7d4e9f02a58"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ACTIVE = "('queued', 'scheduled', 'uploading', 'pending_approval')"


def upgrade() -> None:
    op.add_column("users", sa.Column("can_approve_public", sa.Boolean(), server_default=sa.false()))

    op.create_table(
        "tenant_settings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("tenant_id", sa.String(length=100), nullable=False),
        sa.Column("settings_json", sa.JSON(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id"),
    )
    op.create_index("ix_tenant_settings_tenant_id", "tenant_settings", ["tenant_id"])

    op.create_table(
        "youtube_api_quota",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("quota_date", sa.String(length=10), nullable=False),
        sa.Column("units_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("alert_sent", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("quota_date"),
    )

    op.add_column("publish_jobs", sa.Column("approved_by", sa.Integer(), nullable=True))
    op.add_column("publish_jobs", sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("publish_jobs", sa.Column("denial_reason", sa.Text(), nullable=True))
    op.add_column("publish_jobs", sa.Column("blocked_reason", sa.String(length=50), nullable=True))
    op.add_column("publish_jobs", sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("publish_jobs", sa.Column("content_id_check", sa.JSON(), nullable=True))

    op.drop_index("uq_publish_active", table_name="publish_jobs")
    op.create_index(
        "uq_publish_active",
        "publish_jobs",
        ["job_id", "kind"],
        unique=True,
        postgresql_where=sa.text(f"status IN {_ACTIVE}"),
        sqlite_where=sa.text(f"status IN {_ACTIVE}"),
    )

    op.create_index("ix_audit_action_created", "audit_log", ["action", "created_at"])
    op.create_index("ix_audit_user_created", "audit_log", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_audit_user_created", table_name="audit_log")
    op.drop_index("ix_audit_action_created", table_name="audit_log")
    op.drop_index("uq_publish_active", table_name="publish_jobs")
    op.create_index(
        "uq_publish_active",
        "publish_jobs",
        ["job_id", "kind"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'scheduled', 'uploading')"),
        sqlite_where=sa.text("status IN ('queued', 'scheduled', 'uploading')"),
    )
    for col in ("content_id_check", "next_attempt_at", "blocked_reason",
                "denial_reason", "approved_at", "approved_by"):
        op.drop_column("publish_jobs", col)
    op.drop_table("youtube_api_quota")
    op.drop_index("ix_tenant_settings_tenant_id", table_name="tenant_settings")
    op.drop_table("tenant_settings")
    op.drop_column("users", "can_approve_public")
