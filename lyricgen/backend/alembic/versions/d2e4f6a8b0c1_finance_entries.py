"""finance_entries: renglones de la planilla de finanzas del admin.

Revision ID: d2e4f6a8b0c1
Revises: c1d3e5f7a9b2

Tabla NUEVA, sin claves foráneas y con dos índices. Sólo la escriben los
endpoints super-admin de ``finance_ledger.py``; nada del pipeline la lee, así
que crearla no toca el camino de un render ni toma locks de ``jobs``.

Idempotente: ``f3b5d7e9a1c3`` crea las tablas ausentes desde la metadata
*actual* del modelo, así que en una base nueva la tabla puede existir ya.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "d2e4f6a8b0c1"
down_revision: Union[str, Sequence[str], None] = "c1d3e5f7a9b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if op.get_context().dialect.name == "postgresql":
        op.execute(
            "CREATE TABLE IF NOT EXISTS finance_entries ("
            "id SERIAL PRIMARY KEY, "
            "period VARCHAR(7) NOT NULL, "
            "section VARCHAR(24) NOT NULL, "
            "category VARCHAR(40), "
            "provider VARCHAR(80), "
            "concept VARCHAR(200), "
            "amount_usd DOUBLE PRECISION, "
            "basis VARCHAR(16), "
            "contract VARCHAR(80), "
            "evidence TEXT, "
            "note TEXT, "
            "payload JSONB, "
            "created_by_user_id INTEGER, "
            "updated_by_user_id INTEGER, "
            "created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "deleted_at TIMESTAMPTZ)"
        )
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_finance_entries_period "
            "ON finance_entries (period)"
        )
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_finance_entries_section "
            "ON finance_entries (section)"
        )
        return
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("finance_entries"):
        return
    op.create_table(
        "finance_entries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("period", sa.String(length=7), nullable=False),
        sa.Column("section", sa.String(length=24), nullable=False),
        sa.Column("category", sa.String(length=40), nullable=True),
        sa.Column("provider", sa.String(length=80), nullable=True),
        sa.Column("concept", sa.String(length=200), nullable=True),
        sa.Column("amount_usd", sa.Float(), nullable=True),
        sa.Column("basis", sa.String(length=16), nullable=True),
        sa.Column("contract", sa.String(length=80), nullable=True),
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=True,
        ),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("updated_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_finance_entries_period", "finance_entries", ["period"])
    op.create_index("ix_finance_entries_section", "finance_entries", ["section"])


def downgrade() -> None:
    op.drop_index("ix_finance_entries_section", table_name="finance_entries")
    op.drop_index("ix_finance_entries_period", table_name="finance_entries")
    op.drop_table("finance_entries")
