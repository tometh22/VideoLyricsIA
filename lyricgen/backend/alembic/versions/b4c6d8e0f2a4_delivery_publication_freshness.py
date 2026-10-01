"""Track which render each delivery is actually serving.

The portal rebuilds the R2 key from (tenant, job_id, file_type) and signs
it on demand, so a re-render replaces the client's download in place with
no trace on the row: same label, same added_at, same "approved by UMG"
pill over content they never reviewed. These columns let the row describe
the cut it serves, so a re-send of the same render is distinguishable from
a genuinely new version, and a change request can be closed by the
publication that answered it.

Idempotent on PostgreSQL on purpose. The `deliveries` rows live in the
PRODUCTION database even when the admin writing them runs in staging
(`DELIVERIES_DATABASE_URL`), while Alembic runs against each environment's
OWN database. So these columns have to be installed in production ahead of
the staging deploy — otherwise staging selects columns that do not exist
there — and this revision still has to run cleanly later when production
finally reaches it. `ADD COLUMN IF NOT EXISTS` makes both orders safe.

Revision ID: b4c6d8e0f2a4
Revises: c9d1e3f5a7b9
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "b4c6d8e0f2a4"
down_revision: Union[str, Sequence[str], None] = "c9d1e3f5a7b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (tabla, columna, tipo SQL, sufijo). `published_revision` arranca en 1:
# las filas existentes se publicaron una vez y lo que sirven es lo último
# que el cliente vio.
_COLUMNS = (
    ("deliveries", "published_render_fingerprint", "VARCHAR(64)", ""),
    ("deliveries", "published_revision", "INTEGER", " DEFAULT 1 NOT NULL"),
    ("deliveries", "content_updated_at", "TIMESTAMPTZ", ""),
    ("deliveries", "stale_since", "TIMESTAMPTZ", ""),
    ("deliveries", "stale_reason", "VARCHAR(40)", ""),
    ("delivery_change_requests", "resolved_by_revision", "INTEGER", ""),
    ("delivery_change_requests", "resolution_source", "VARCHAR(20)", ""),
)

_SA_TYPES = {
    "VARCHAR(64)": sa.String(length=64),
    "VARCHAR(40)": sa.String(length=40),
    "VARCHAR(20)": sa.String(length=20),
    "INTEGER": sa.Integer(),
    "TIMESTAMPTZ": sa.DateTime(timezone=True),
}


def upgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        for table, column, sql_type, suffix in _COLUMNS:
            op.execute(
                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "
                f"{column} {sql_type}{suffix}"
            )
        return
    for table, column, sql_type, suffix in _COLUMNS:
        op.add_column(table, sa.Column(
            column, _SA_TYPES[sql_type],
            nullable="NOT NULL" not in suffix,
            server_default="1" if "DEFAULT 1" in suffix else None,
        ))


def downgrade() -> None:
    for table, column, _sql_type, _suffix in reversed(_COLUMNS):
        op.drop_column(table, column)
