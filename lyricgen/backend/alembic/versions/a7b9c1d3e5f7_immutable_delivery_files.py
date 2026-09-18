"""Pin published portal files independently of mutable working renders.

The shared deliveries DB must receive this additive migration before deploying
any API/worker using the new model (including staging's external portal DB).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'a7b9c1d3e5f7'
down_revision = 'e6a8c0d2f4b6'
branch_labels = None
depends_on = None


def upgrade():
    if 'published_file_keys' not in {c['name'] for c in sa.inspect(op.get_bind()).get_columns('deliveries')}:
        op.add_column('deliveries', sa.Column('published_file_keys', sa.JSON().with_variant(postgresql.JSONB(), 'postgresql'), nullable=True))


def downgrade():
    op.drop_column('deliveries', 'published_file_keys')
