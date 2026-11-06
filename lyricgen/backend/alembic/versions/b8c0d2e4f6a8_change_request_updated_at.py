"""Persist the latest change request lifecycle timestamp.

Revision ID: b8c0d2e4f6a8
Revises: a7b9c1d3e5f7
"""
from alembic import op
import sqlalchemy as sa


revision = "b8c0d2e4f6a8"
down_revision = "a7b9c1d3e5f7"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_context().dialect.name == "postgresql":
        op.execute(
            "ALTER TABLE delivery_change_requests "
            "ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ"
        )
        # Preserve the most meaningful legacy timestamp where available.
        op.execute(
            "UPDATE delivery_change_requests "
            "SET updated_at = GREATEST(submitted_at, "
            "COALESCE(resolved_at, submitted_at)) "
            "WHERE updated_at IS NULL"
        )
        op.execute(
            "ALTER TABLE delivery_change_requests "
            "ALTER COLUMN updated_at SET DEFAULT CURRENT_TIMESTAMP"
        )
        op.execute(
            "ALTER TABLE delivery_change_requests "
            "ALTER COLUMN updated_at SET NOT NULL"
        )
        return

    op.add_column(
        "delivery_change_requests",
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE delivery_change_requests SET updated_at = "
        "CASE WHEN resolved_at IS NOT NULL AND resolved_at > submitted_at "
        "THEN resolved_at ELSE submitted_at END"
    )


def downgrade():
    op.drop_column("delivery_change_requests", "updated_at")
