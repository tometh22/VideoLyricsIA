"""Join production and staging product-category histories without data changes.

Both parent revisions install the same nullable delivery product snapshot
idempotently. Existing production databases advance through the staging
additions; staging databases acknowledge the production revision. No table or
column is removed in either direction at this merge point.
"""

revision = "f4a6c8e0b2d4"
down_revision = ("a6c8e0f2b4d6", "b7d9f1a3c5e7")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
