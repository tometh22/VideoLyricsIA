"""Archive for reviewer-requested re-transcriptions in another language.

Additive only: one new append-only table. Existing editor documents, versions
and correction observations are untouched.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "a7c3e9f1b5d2"
down_revision = "e3f621bbc22f"
branch_labels = None
depends_on = None


def _json():
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade():
    op.create_table(
        "editor_retranscriptions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "job_id",
            sa.String(12),
            sa.ForeignKey("jobs.job_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.String(100), nullable=False),
        sa.Column("attempt_id", sa.String(36), nullable=False),
        sa.Column("requested_language", sa.String(8), nullable=False),
        sa.Column("previous_language", sa.String(16), nullable=True),
        sa.Column("base_revision", sa.Integer(), nullable=False),
        sa.Column("archived_version_id", sa.String(36), nullable=True),
        sa.Column("archived_original_segments", _json(), nullable=True),
        sa.Column("archived_machine_evidence", _json(), nullable=True),
        sa.Column("archived_transcription_quality", _json(), nullable=True),
        sa.Column(
            "status", sa.String(20), nullable=False, server_default="pending",
        ),
        sa.Column("result_revision", sa.Integer(), nullable=True),
        sa.Column("error", sa.String(500), nullable=True),
        sa.Column(
            "requested_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_editor_retranscriptions_job_id", "editor_retranscriptions", ["job_id"],
    )
    op.create_index(
        "ix_editor_retranscriptions_tenant_id", "editor_retranscriptions", ["tenant_id"],
    )
    op.create_index(
        "ix_editor_retranscriptions_job_created",
        "editor_retranscriptions", ["job_id", "created_at"],
    )
    op.create_index(
        "ix_editor_retranscriptions_attempt",
        "editor_retranscriptions", ["attempt_id"], unique=True,
    )


def downgrade():
    op.drop_table("editor_retranscriptions")
