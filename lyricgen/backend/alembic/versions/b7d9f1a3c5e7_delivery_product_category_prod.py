"""Snapshot UMG portal product on deliveries (production chain).

Revision ID: b7d9f1a3c5e7
Revises: ba888d1665d8
"""

from alembic import op
import sqlalchemy as sa

revision = "b7d9f1a3c5e7"
down_revision = "ba888d1665d8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {col["name"] for col in sa.inspect(op.get_bind()).get_columns("deliveries")}
    if "product_category" not in columns:
        op.add_column("deliveries", sa.Column("product_category", sa.String(32), nullable=True))
    op.execute("UPDATE deliveries SET product_category = 'artists_art' WHERE label = 'Art Track' AND product_category IS NULL")
    indexes = {idx["name"] for idx in sa.inspect(op.get_bind()).get_indexes("deliveries")}
    if "ix_deliveries_product_category" not in indexes:
        op.create_index("ix_deliveries_product_category", "deliveries", ["product_category"])


def downgrade() -> None:
    op.drop_index("ix_deliveries_product_category", table_name="deliveries")
    op.drop_column("deliveries", "product_category")
