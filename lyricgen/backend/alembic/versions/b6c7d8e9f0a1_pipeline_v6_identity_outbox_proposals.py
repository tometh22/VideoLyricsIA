"""pipeline v6 audio identity, transactional outbox and editor proposals

Revision ID: b6c7d8e9f0a1
Revises: a8c1e4f7b2d9
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "b6c7d8e9f0a1"
down_revision = "a8c1e4f7b2d9"
branch_labels = None
depends_on = None


def _portable_json():
    """JSON on SQLite/tests, JSONB on PostgreSQL production."""
    return sa.JSON().with_variant(
        postgresql.JSONB(astext_type=sa.Text()), "postgresql",
    )


def upgrade():
    op.add_column("jobs", sa.Column("input_audio_sha256", sa.String(64), nullable=True))
    op.add_column("jobs", sa.Column("input_audio_etag", sa.Text(), nullable=True))
    op.add_column(
        "jobs",
        sa.Column("audio_revision", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.add_column("jobs", sa.Column("active_quality_attempt_id", sa.String(160), nullable=True))
    op.add_column(
        "editor_documents",
        sa.Column("quality_proposal", _portable_json(), nullable=True),
    )
    op.create_table(
        "job_outbox_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "job_id", sa.String(12),
            sa.ForeignKey("jobs.job_id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("dedupe_key", sa.String(160), nullable=False),
        sa.Column("payload", _portable_json(), nullable=False),
        sa.Column("status", sa.String(20), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processing_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processing_token", sa.String(36), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(160), nullable=True),
    )
    op.create_index("ix_job_outbox_events_job_id", "job_outbox_events", ["job_id"])
    op.create_index("ix_job_outbox_events_event_type", "job_outbox_events", ["event_type"])
    op.create_index("ix_job_outbox_events_dedupe_key", "job_outbox_events", ["dedupe_key"], unique=True)
    op.create_index(
        "ix_job_outbox_status_available", "job_outbox_events", ["status", "available_at"],
    )


def downgrade():
    op.drop_table("job_outbox_events")
    op.drop_column("editor_documents", "quality_proposal")
    op.drop_column("jobs", "active_quality_attempt_id")
    op.drop_column("jobs", "audio_revision")
    op.drop_column("jobs", "input_audio_etag")
    op.drop_column("jobs", "input_audio_sha256")
