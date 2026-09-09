"""Reconcile objects skipped by a historical Alembic stamp.

Production was stamped at ``a1c3e5f7b902`` even though a subset of the
preceding runtime tables and columns was never installed.  The normal
revision history cannot replay those revisions because the database already
claims to be past them.  This migration is deliberately additive and
idempotent: it creates only absent tables from the current model metadata and
adds only the known absent columns needed by the schema gate.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "f3b5d7e9a1c3"
down_revision: Union[str, Sequence[str], None] = "f2a4b6c8d0e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _json():
    return postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")


def _add_column_if_missing(table: str, column: sa.Column) -> None:
    inspector = sa.inspect(op.get_bind())
    present = {item["name"] for item in inspector.get_columns(table)}
    if column.name not in present:
        op.add_column(table, column)


def _create_index_if_missing(name: str, table: str, columns: list[str], *, unique=False) -> None:
    inspector = sa.inspect(op.get_bind())
    present = {item["name"] for item in inspector.get_indexes(table)}
    if name not in present:
        op.create_index(name, table, columns, unique=unique)


def upgrade() -> None:
    # The model metadata is the authoritative shape for tables whose earlier
    # migrations were stamped without executing.  checkfirst keeps this safe
    # for databases that already have the complete table set.
    from database import Base

    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, checkfirst=True)

    _add_column_if_missing(
        "jobs",
        sa.Column("workload_class", sa.String(length=16), nullable=False,
                  server_default="interactive"),
    )
    _add_column_if_missing("jobs", sa.Column("campaign_id", sa.String(length=12), nullable=True))
    _add_column_if_missing(
        "jobs", sa.Column("campaign_item_id", sa.String(length=36), nullable=True),
    )
    _add_column_if_missing(
        "jobs",
        sa.Column("machine_snapshot_required", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
    )
    _add_column_if_missing(
        "jobs",
        sa.Column(
            "delivery_qc", postgresql.JSONB(astext_type=sa.Text()).with_variant(
                sa.JSON(), "sqlite"
            ), nullable=True,
        ),
    )
    _add_column_if_missing(
        "editor_documents", sa.Column("lock_session_id", sa.String(length=64), nullable=True),
    )
    _add_column_if_missing("editor_documents", sa.Column("machine_evidence", _json(), nullable=True))

    _create_index_if_missing("ix_jobs_workload_class", "jobs", ["workload_class"])
    _create_index_if_missing("ix_jobs_campaign_id", "jobs", ["campaign_id"])
    _create_index_if_missing(
        "ix_jobs_campaign_item_id", "jobs", ["campaign_item_id"], unique=True,
    )


def downgrade() -> None:
    # This is a reconciliation for a stamped database. Removing the repaired
    # objects would reintroduce the production inconsistency, so rollback is
    # intentionally a no-op; the original revisions remain the owners of
    # their schema objects.
    pass
