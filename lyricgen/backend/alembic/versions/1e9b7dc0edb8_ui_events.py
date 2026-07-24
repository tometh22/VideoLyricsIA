"""ui_events (telemetría de comportamiento del wizard)

Revision ID: 1e9b7dc0edb8
Revises: ab12cd34ef56
Create Date: 2026-06-10 12:00:00.000000

Tabla de eventos de UI para el panel Insights (super-admin): cada fila es
una acción del usuario en el wizard (cambio de paso, selección de fondo,
generate, etc.), alimentada por POST /telemetry/events en batches
best-effort. Gateada por TELEMETRY_ENABLED (default off) — la tabla nace
vacía y solo se llena cuando la flag está prendida.

Puramente aditiva: segura en rolling deploy (releaseCommand de Railway).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "1e9b7dc0edb8"
down_revision: Union[str, Sequence[str], None] = "ab12cd34ef56"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ui_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.String(length=100), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("event_data", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ui_events_user_id", "ui_events", ["user_id"])
    op.create_index("ix_ui_events_tenant_id", "ui_events", ["tenant_id"])
    op.create_index(
        "ix_ui_events_user_created",
        "ui_events",
        ["user_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_ui_events_type_created",
        "ui_events",
        ["event_type", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_ui_events_type_created", table_name="ui_events")
    op.drop_index("ix_ui_events_user_created", table_name="ui_events")
    op.drop_index("ix_ui_events_tenant_id", table_name="ui_events")
    op.drop_index("ix_ui_events_user_id", table_name="ui_events")
    op.drop_table("ui_events")
