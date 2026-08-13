"""Add durable editor snapshots and optimistic revisions.

Revision ID: 9c1f2e7d4a11
Revises: 8802e2187632
"""

from alembic import op
import sqlalchemy as sa


revision = "9c1f2e7d4a11"
down_revision = "8802e2187632"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("jobs", sa.Column("segments_json", sa.JSON(), nullable=True))
    op.add_column(
        "jobs",
        sa.Column("segments_revision", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_table(
        "editor_versions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=12), nullable=False),
        sa.Column("tenant_id", sa.String(length=100), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("segments", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(length=30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.job_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_editor_versions_job_revision",
        "editor_versions",
        ["job_id", "revision"],
        unique=True,
    )
    op.create_index(
        "ix_editor_versions_job_created",
        "editor_versions",
        ["job_id", "created_at"],
        unique=False,
    )
    op.create_index("ix_editor_versions_tenant_id", "editor_versions", ["tenant_id"], unique=False)


def downgrade():
    op.drop_index("ix_editor_versions_tenant_id", table_name="editor_versions")
    op.drop_index("ix_editor_versions_job_created", table_name="editor_versions")
    op.drop_index("ix_editor_versions_job_revision", table_name="editor_versions")
    op.drop_table("editor_versions")
    op.drop_column("jobs", "segments_revision")
    op.drop_column("jobs", "segments_json")
