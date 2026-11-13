"""Add schema for reversible automated change requests and acoustic QC."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "c0e2f4a6b8d0"
down_revision = "f4a6c8e0b2d4"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    job_columns = {c["name"] for c in inspector.get_columns("jobs")}
    if "acoustic_review" not in job_columns:
        op.add_column("jobs", sa.Column(
            "acoustic_review",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=True,
        ))
    proposal_columns = {c["name"]: c for c in inspector.get_columns("change_request_proposals")}
    with op.batch_alter_table("change_request_proposals") as batch:
        if "actor_kind" not in proposal_columns:
            batch.add_column(sa.Column("actor_kind", sa.String(16), nullable=False, server_default="user"))
        if "created_by" in proposal_columns and not proposal_columns["created_by"]["nullable"]:
            batch.alter_column("created_by", existing_type=sa.Integer(), nullable=True)


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT count(*) FROM change_request_proposals WHERE created_by IS NULL")).scalar():
        raise RuntimeError("system_actor_history_requires_forward_fix")
    op.drop_column("jobs", "acoustic_review")
    with op.batch_alter_table("change_request_proposals") as batch:
        batch.drop_column("actor_kind")
        batch.alter_column("created_by", existing_type=sa.Integer(), nullable=False)
