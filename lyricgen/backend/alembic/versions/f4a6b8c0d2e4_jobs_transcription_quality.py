"""persist transcription quality gate verdict

Revision ID: f4a6b8c0d2e4
Revises: c3d4e5f6a7b8
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "f4a6b8c0d2e4"
down_revision: Union[str, Sequence[str], None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "jobs", sa.Column("transcription_quality", postgresql.JSONB(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("jobs", "transcription_quality")
