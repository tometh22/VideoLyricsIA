"""deleted_job_lyrics_archive: survive delete_job/bulk_delete_jobs cascade

Revision ID: d2e4f6a8b1c3
Revises: 310138eb0e54
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "d2e4f6a8b1c3"
down_revision: Union[str, Sequence[str], None] = "310138eb0e54"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "deleted_job_lyrics_archive",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        # No ForeignKey to jobs.job_id on purpose — this table must outlive
        # the job it was copied from (delete_job/bulk_delete_jobs hard-delete
        # the Job row, cascading editor_documents/editor_versions with it).
        sa.Column("job_id", sa.String(length=12), nullable=False),
        sa.Column("tenant_id", sa.String(length=100), nullable=False),
        sa.Column("artist", sa.String(length=255), nullable=True),
        sa.Column("song_title", sa.String(length=500), nullable=True),
        sa.Column("job_status_at_deletion", sa.String(length=20), nullable=False),
        sa.Column(
            "segments",
            postgresql.JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=False,
        ),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=False),
        # Nullable, no ForeignKey to users.id — the archive must not depend
        # on the acting user's row surviving.
        sa.Column("deleted_by_user_id", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_deleted_job_lyrics_archive_job_id",
        "deleted_job_lyrics_archive",
        ["job_id"],
    )
    op.create_index(
        "ix_deleted_job_lyrics_archive_archived_at",
        "deleted_job_lyrics_archive",
        ["archived_at"],
    )
    op.create_index(
        "ix_deleted_job_lyrics_archive_tenant_job",
        "deleted_job_lyrics_archive",
        ["tenant_id", "job_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_deleted_job_lyrics_archive_tenant_job",
        table_name="deleted_job_lyrics_archive",
    )
    op.drop_index(
        "ix_deleted_job_lyrics_archive_archived_at",
        table_name="deleted_job_lyrics_archive",
    )
    op.drop_index(
        "ix_deleted_job_lyrics_archive_job_id",
        table_name="deleted_job_lyrics_archive",
    )
    op.drop_table("deleted_job_lyrics_archive")
