"""publish_jobs: background YouTube publish queue

Revision ID: c7d4e9f02a58
Revises: b3f2c8a91d47
Create Date: 2026-07-06 19:00:00.000000

One row per uploaded asset (video / short). The partial unique index
uq_publish_active is the cross-worker duplicate-publish guarantee: at
most one non-terminal row per (job_id, kind).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7d4e9f02a58"
down_revision: Union[str, Sequence[str], None] = "b3f2c8a91d47"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ACTIVE = "('queued', 'scheduled', 'uploading')"


def upgrade() -> None:
    op.create_table(
        "publish_jobs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.String(length=12), nullable=False),
        sa.Column("tenant_id", sa.String(length=100), nullable=False),
        sa.Column("channel_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=10), nullable=False, server_default="video"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="queued"),
        sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("privacy", sa.String(length=10), nullable=False, server_default="unlisted"),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("publish_at_youtube", sa.DateTime(timezone=True), nullable=True),
        sa.Column("video_id", sa.String(length=20), nullable=True),
        sa.Column("video_url", sa.String(length=255), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.job_id"]),
        sa.ForeignKeyConstraint(["channel_id"], ["youtube_channels.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_publish_jobs_job", "publish_jobs", ["job_id"])
    op.create_index("ix_publish_jobs_tenant_id", "publish_jobs", ["tenant_id"])
    op.create_index("ix_publish_jobs_created_at", "publish_jobs", ["created_at"])
    op.create_index(
        "ix_publish_jobs_status_scheduled", "publish_jobs", ["status", "scheduled_at"],
    )
    op.create_index(
        "uq_publish_active",
        "publish_jobs",
        ["job_id", "kind"],
        unique=True,
        postgresql_where=sa.text(f"status IN {_ACTIVE}"),
        sqlite_where=sa.text(f"status IN {_ACTIVE}"),
    )


def downgrade() -> None:
    op.drop_index("uq_publish_active", table_name="publish_jobs")
    op.drop_index("ix_publish_jobs_status_scheduled", table_name="publish_jobs")
    op.drop_index("ix_publish_jobs_created_at", table_name="publish_jobs")
    op.drop_index("ix_publish_jobs_tenant_id", table_name="publish_jobs")
    op.drop_index("ix_publish_jobs_job", table_name="publish_jobs")
    op.drop_table("publish_jobs")
