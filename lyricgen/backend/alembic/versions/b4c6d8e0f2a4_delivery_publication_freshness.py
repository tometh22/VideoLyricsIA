"""Track which render each delivery is actually serving.

The portal rebuilds the R2 key from (tenant, job_id, file_type) and signs
it on demand, so a re-render replaces the client's download in place with
no trace on the row: same label, same added_at, same "approved by UMG"
pill over content they never reviewed. These columns let the row describe
the cut it serves, so a re-send of the same render is distinguishable from
a genuinely new version, and a change request can be closed by the
publication that answered it.

Revision ID: b4c6d8e0f2a4
Revises: c9d1e3f5a7b9
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "b4c6d8e0f2a4"
down_revision: Union[str, Sequence[str], None] = "c9d1e3f5a7b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "deliveries",
        sa.Column("published_render_fingerprint", sa.String(length=64), nullable=True),
    )
    # Existing rows are revision 1: they were published once and whatever
    # they serve is what the client last saw.
    op.add_column(
        "deliveries",
        sa.Column(
            "published_revision",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    op.add_column(
        "deliveries",
        sa.Column("content_updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "deliveries",
        sa.Column("stale_since", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "deliveries",
        sa.Column("stale_reason", sa.String(length=40), nullable=True),
    )
    op.add_column(
        "delivery_change_requests",
        sa.Column("resolved_by_revision", sa.Integer(), nullable=True),
    )
    op.add_column(
        "delivery_change_requests",
        sa.Column("resolution_source", sa.String(length=20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("delivery_change_requests", "resolution_source")
    op.drop_column("delivery_change_requests", "resolved_by_revision")
    op.drop_column("deliveries", "stale_reason")
    op.drop_column("deliveries", "stale_since")
    op.drop_column("deliveries", "content_updated_at")
    op.drop_column("deliveries", "published_revision")
    op.drop_column("deliveries", "published_render_fingerprint")
