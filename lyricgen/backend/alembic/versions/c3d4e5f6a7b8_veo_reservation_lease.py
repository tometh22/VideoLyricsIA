"""expire abandoned pre-submit Veo budget reservations

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ai_provenance",
        sa.Column(
            "reservation_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_ai_provenance_reservation_expires_at",
        "ai_provenance",
        ["reservation_expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ai_provenance_reservation_expires_at",
        table_name="ai_provenance",
    )
    op.drop_column("ai_provenance", "reservation_expires_at")
