"""Tenant-scoped coordination permissions, assignments and activity."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "b8c0d2e4f6a8"
down_revision = "a7b9c1d3e5f7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "team_coordinators",
        sa.Column("tenant_id", sa.String(100), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "granted_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "team_assignments",
        sa.Column(
            "item_id",
            sa.String(36),
            sa.ForeignKey("batch_campaign_items.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("tenant_id", sa.String(100), nullable=False),
        sa.Column("assignee_id", sa.Integer(), sa.ForeignKey("users.id")),
        sa.Column("priority", sa.String(10), nullable=False),
        sa.Column("due_date", sa.Date()),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("blocked_reason", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False
        ),
    )
    op.create_index("ix_team_assignments_tenant_id", "team_assignments", ["tenant_id"])
    op.create_index(
        "ix_team_assignments_assignee_id", "team_assignments", ["assignee_id"]
    )
    op.create_table(
        "team_activity",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.String(100), nullable=False),
        sa.Column(
            "item_id",
            sa.String(36),
            sa.ForeignKey("batch_campaign_items.id", ondelete="CASCADE"),
        ),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column(
            "detail",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_team_activity_tenant_created", "team_activity", ["tenant_id", "created_at"]
    )


def downgrade():
    op.drop_table("team_activity")
    op.drop_table("team_assignments")
    op.drop_table("team_coordinators")
