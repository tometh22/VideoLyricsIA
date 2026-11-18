"""review_signal_records: persist machine review signals for later evaluation.

Revision ID: c1d3e5f7a9b2
Revises: ba888d1665d8

Two review signals were computed and then lost: the lyric_review items (they
lived only in an in-process LRU cache) and the repetition_reconcile proposals
(declined ones went only to the log). This table keeps them so they can be
crossed later with the client's change requests (precision/recall per signal).

Purely additive: a NEW table with no foreign keys (inserts never lock the
``jobs`` row) and three indexes. Nothing in the product reads it, and the code
only writes it behind ``REVIEW_SIGNALS_PERSIST_ENABLED`` (default off).

Idempotent: ``f3b5d7e9a1c3`` creates absent tables from the *current* model
metadata, so on a fresh database the table may already exist when this runs.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "c1d3e5f7a9b2"
down_revision: Union[str, Sequence[str], None] = "ba888d1665d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        op.execute(
            "CREATE TABLE IF NOT EXISTS review_signal_records ("
            "id SERIAL PRIMARY KEY, "
            "job_id VARCHAR(12) NOT NULL, "
            "tenant_id VARCHAR(100), "
            "kind VARCHAR(32) NOT NULL, "
            "item_type VARCHAR(40) NOT NULL, "
            "decision VARCHAR(16) NOT NULL, "
            "reason VARCHAR(80), "
            "editor_revision INTEGER, "
            "segments_hash VARCHAR(64), "
            "pipeline_release VARCHAR(64), "
            "line_index INTEGER, "
            "start_s DOUBLE PRECISION, "
            "end_s DOUBLE PRECISION, "
            "payload JSONB, "
            "dedupe_key VARCHAR(64) NOT NULL, "
            "created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "CONSTRAINT uq_review_signal_records_dedupe_key UNIQUE (dedupe_key))"
        )
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_review_signal_records_job_id "
            "ON review_signal_records (job_id)"
        )
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_review_signal_records_kind_created "
            "ON review_signal_records (kind, created_at)"
        )
        return
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("review_signal_records"):
        return
    op.create_table(
        "review_signal_records",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.String(length=12), nullable=False),
        sa.Column("tenant_id", sa.String(length=100), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("item_type", sa.String(length=40), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=80), nullable=True),
        sa.Column("editor_revision", sa.Integer(), nullable=True),
        sa.Column("segments_hash", sa.String(length=64), nullable=True),
        sa.Column("pipeline_release", sa.String(length=64), nullable=True),
        sa.Column("line_index", sa.Integer(), nullable=True),
        sa.Column("start_s", sa.Float(), nullable=True),
        sa.Column("end_s", sa.Float(), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=True,
        ),
        sa.Column("dedupe_key", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key", name="uq_review_signal_records_dedupe_key"),
    )
    op.create_index(
        "ix_review_signal_records_job_id", "review_signal_records", ["job_id"],
    )
    op.create_index(
        "ix_review_signal_records_kind_created", "review_signal_records",
        ["kind", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_review_signal_records_kind_created", table_name="review_signal_records",
    )
    op.drop_index("ix_review_signal_records_job_id", table_name="review_signal_records")
    op.drop_table("review_signal_records")
