"""Recoverable campaign discard record."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "c9d1e3f5a7b9"
down_revision = "f8c9d0e1a2b3"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("batch_campaign_items", sa.Column(
        "discard_record", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=True,
    ))


def downgrade():
    op.drop_column("batch_campaign_items", "discard_record")
