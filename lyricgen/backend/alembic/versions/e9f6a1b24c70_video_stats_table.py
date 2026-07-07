"""video_stats: per-video daily YouTube metrics

Revision ID: e9f6a1b24c70
Revises: d8e5f0a13b69
Create Date: 2026-07-06 22:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e9f6a1b24c70"
down_revision: Union[str, Sequence[str], None] = "d8e5f0a13b69"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "video_stats",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("video_id", sa.String(length=20), nullable=False),
        sa.Column("publish_job_id", sa.Integer(), nullable=True),
        sa.Column("tenant_id", sa.String(length=100), nullable=False),
        sa.Column("stat_date", sa.String(length=10), nullable=False),
        sa.Column("views", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("likes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("comments", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("estimated_minutes_watched", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(length=20), nullable=False, server_default="data_api"),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["publish_job_id"], ["publish_jobs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("video_id", "stat_date", name="uq_video_stats_day"),
    )
    op.create_index("ix_video_stats_video_id", "video_stats", ["video_id"])
    op.create_index("ix_video_stats_tenant_date", "video_stats", ["tenant_id", "stat_date"])


def downgrade() -> None:
    op.drop_index("ix_video_stats_tenant_date", table_name="video_stats")
    op.drop_index("ix_video_stats_video_id", table_name="video_stats")
    op.drop_table("video_stats")
