"""persist mandatory pre-human transcription evidence

Revision ID: e4b8c2d6f0a1
Revises: e3a7c9b1d5f0
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "e4b8c2d6f0a1"
down_revision: Union[str, Sequence[str], None] = "e3a7c9b1d5f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _json():
    return postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column(
            "machine_snapshot_required", sa.Boolean(),
            server_default=sa.false(), nullable=False,
        ),
    )
    op.add_column(
        "editor_documents",
        sa.Column("machine_evidence", _json(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("editor_documents", "machine_evidence")
    op.drop_column("jobs", "machine_snapshot_required")

