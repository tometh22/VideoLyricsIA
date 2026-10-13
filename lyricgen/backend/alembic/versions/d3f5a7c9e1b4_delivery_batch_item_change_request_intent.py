"""Remember which client requests a campaign send was reviewed to close.

Revision ID: d3f5a7c9e1b4
Revises: b8c0d2e4f6a8

Local table only (never the shared portal database). Additive and nullable, so
an older worker that ignores it keeps working.
"""
from alembic import op
import sqlalchemy as sa


revision = "d3f5a7c9e1b4"
down_revision = "b8c0d2e4f6a8"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_context().dialect.name == "postgresql":
        op.execute(
            "ALTER TABLE delivery_batch_items "
            "ADD COLUMN IF NOT EXISTS change_request_intent JSONB"
        )
        return
    op.add_column("delivery_batch_items", sa.Column("change_request_intent", sa.JSON(), nullable=True))


def downgrade():
    if op.get_context().dialect.name == "postgresql":
        op.execute("ALTER TABLE delivery_batch_items DROP COLUMN IF EXISTS change_request_intent")
        return
    op.drop_column("delivery_batch_items", "change_request_intent")
