"""Add revision-bound delivery change request proposals.

Revision ID: e6a8c0d2f4b6
Revises: b4c6d8e0f2a4
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "e6a8c0d2f4b6"
down_revision: Union[str, Sequence[str], None] = "b4c6d8e0f2a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ``f3b5d7e9a1c3`` repairs historically stamped databases by creating
    # tables that are present in the *current* model metadata.  On a clean
    # install that older repair can therefore create this future-owned table
    # before Alembic reaches this revision.  Production (where f3 is already
    # applied) still needs the normal CREATE below.  Keep both paths safe.
    if "change_request_proposals" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "change_request_proposals",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("portal_id", sa.String(length=32), nullable=False),
        sa.Column("change_request_id", sa.Integer(), nullable=False),
        sa.Column("delivery_id", sa.Integer(), nullable=False),
        sa.Column(
            "job_id", sa.String(length=12),
            sa.ForeignKey("jobs.job_id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("base_revision", sa.Integer(), nullable=False),
        sa.Column("segments_hash", sa.String(length=64), nullable=False),
        sa.Column("segments_content_hash", sa.String(length=64), nullable=False),
        sa.Column("audio_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "audio_sha256", sa.String(length=64), nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column("parser_version", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("operations", sa.JSON(), nullable=False),
        sa.Column("decision_history", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("applied_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_revision", sa.Integer(), nullable=True),
        sa.Column("idempotency_hash", sa.String(length=64), nullable=True),
        sa.UniqueConstraint(
            "portal_id", "change_request_id", "request_sha256", "base_revision",
            name="uq_change_request_proposal_snapshot",
        ),
    )
    op.create_index(
        "ix_crp_request_status", "change_request_proposals",
        ["portal_id", "change_request_id", "status"],
    )
    op.create_index(
        "ix_crp_job_created", "change_request_proposals",
        ["job_id", "created_at"],
    )


def downgrade() -> None:
    if "change_request_proposals" not in sa.inspect(op.get_bind()).get_table_names():
        return
    op.drop_index("ix_crp_job_created", table_name="change_request_proposals")
    op.drop_index("ix_crp_request_status", table_name="change_request_proposals")
    op.drop_table("change_request_proposals")
