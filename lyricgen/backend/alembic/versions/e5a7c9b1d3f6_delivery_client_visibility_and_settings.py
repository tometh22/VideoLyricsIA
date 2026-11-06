"""Per-delivery client visibility and a local system settings table.

Revision ID: e5a7c9b1d3f6
Revises: d3f5a7c9e1b4

``deliveries.client_visibility`` is NULLABLE with NO default and is added with
``IF NOT EXISTS``: the portal rows live in the PRODUCTION database even when an
operator in staging writes them (``DELIVERIES_DATABASE_URL``), so the column must
be safe to install ahead of the code that reads it and safe to re-run. A nullable
column without default does not rewrite the table on PostgreSQL, and code that
ignores it (production before the next promotion) keeps working unchanged.

NULL / 'auto'  hide the client's view while the delivery has unpublished changes
'visible'      always show it
'hidden'       never show it until an operator changes it

``system_settings`` is a LOCAL table (never the shared portal database).
"""
from alembic import op
import sqlalchemy as sa


revision = "e5a7c9b1d3f6"
down_revision = "d3f5a7c9e1b4"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_context().dialect.name == "postgresql":
        op.execute(
            "ALTER TABLE deliveries "
            "ADD COLUMN IF NOT EXISTS client_visibility VARCHAR(10)"
        )
        op.execute(
            "CREATE TABLE IF NOT EXISTS system_settings ("
            "key VARCHAR(80) PRIMARY KEY, "
            "value VARCHAR(200), "
            "updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP, "
            "updated_by_user_id INTEGER)"
        )
        return
    # Non-PostgreSQL (dev/CI): the baseline revision builds from the current models,
    # so either object may already exist.
    inspector = sa.inspect(op.get_bind())
    if "client_visibility" not in {c["name"] for c in inspector.get_columns("deliveries")}:
        op.add_column("deliveries", sa.Column("client_visibility", sa.String(10), nullable=True))
    if not inspector.has_table("system_settings"):
        op.create_table(
            "system_settings",
            sa.Column("key", sa.String(80), primary_key=True),
            sa.Column("value", sa.String(200)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
            sa.Column("updated_by_user_id", sa.Integer()),
        )


def downgrade():
    op.drop_table("system_settings")
    op.drop_column("deliveries", "client_visibility")
