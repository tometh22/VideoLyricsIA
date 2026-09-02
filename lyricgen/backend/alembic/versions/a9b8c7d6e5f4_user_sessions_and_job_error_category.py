"""user sessions (telemetry) + jobs.error_category

Revision ID: a9b8c7d6e5f4
Revises: b7d4e3a2c109
Create Date: 2026-06-02 12:00:00.000000

Two additive pieces for the admin activity dashboard (observabilidad):

1. `user_sessions` — sesiones de uso alimentadas por POST /telemetry/heartbeat
   (frontend manda 1 ping/min mientras la pestaña está visible). Backs
   "tiempo en la app" y "en línea ahora" por usuario. El endpoint está
   gateado por TELEMETRY_ENABLED (default off) así que la tabla nace
   vacía y solo se llena cuando el operador prende la flag.

2. `jobs.error_category` — categoría del error (veo | render | upload |
   timing | validation | timeout | reaper | unknown) que setean los sinks
   de error del pipeline y del reaper vía error_taxonomy.classify_error().
   Nullable, sin backfill: las rows históricas se clasifican a lectura
   con el mismo clasificador (coalesce en /admin/activity).

Ambas operaciones son additive y lock-light (CREATE TABLE de tabla nueva +
ADD COLUMN nullable sin default) — seguras durante rolling deploy.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a9b8c7d6e5f4"
down_revision: Union[str, Sequence[str], None] = "b7d4e3a2c109"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_sessions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.String(length=100), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeats", sa.Integer(), nullable=False, server_default="1"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_user_sessions_user_id", "user_sessions", ["user_id"])
    op.create_index("ix_user_sessions_tenant_id", "user_sessions", ["tenant_id"])
    op.create_index("ix_user_sessions_last_seen_at", "user_sessions", ["last_seen_at"])
    # (user_id, started_at DESC) — la query del heartbeat ("última sesión de
    # este usuario") y la agregación de tiempo-en-app del admin leen por acá.
    dialect = op.get_context().dialect.name
    if dialect == "postgresql":
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_user_sessions_user_started "
            "ON user_sessions (user_id, started_at DESC)"
        )
    else:
        op.create_index(
            "ix_user_sessions_user_started", "user_sessions",
            ["user_id", "started_at"],
        )

    op.add_column(
        "jobs",
        sa.Column("error_category", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("jobs", "error_category")
    op.drop_index("ix_user_sessions_user_started", table_name="user_sessions")
    op.drop_index("ix_user_sessions_last_seen_at", table_name="user_sessions")
    op.drop_index("ix_user_sessions_tenant_id", table_name="user_sessions")
    op.drop_index("ix_user_sessions_user_id", table_name="user_sessions")
    op.drop_table("user_sessions")
