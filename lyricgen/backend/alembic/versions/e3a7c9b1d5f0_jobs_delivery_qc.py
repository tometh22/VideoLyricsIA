"""Persist final-render delivery QC reports.

Revision ID: e3a7c9b1d5f0
Revises: d2e4f6a8b1c3
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "e3a7c9b1d5f0"
down_revision: Union[str, Sequence[str], None] = "d2e4f6a8b1c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column(
            "delivery_qc",
            postgresql.JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("jobs", "delivery_qc")
