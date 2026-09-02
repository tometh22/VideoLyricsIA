"""cost_daily + cost_collection_runs: costo diario crudo y estado de la recolección

Revision ID: d7c2f9a41b83
Revises: e3a7c9b1d5f0
Create Date: 2026-08-31 12:00:00.000000

Backs the daily cost panel. `cost_snapshots` (mensual, una fila por
proveedor y mes) se conserva sin tocar: es la foto que compara contra la
factura. Estas dos tablas son el grano fino.

Por qué DOS tablas y no una
---------------------------
`cost_collection_runs` registra el INTENTO; `cost_daily` registra el HECHO.
Sin la primera, un día que el proveedor no contestó no tiene filas y se
dibuja igual que un día barato — el total del mes baja en silencio y el
panel muestra una caída que parece una buena noticia. El requisito del
panel es que se pueda confiar en él sin auditarlo a mano cada mes, así que
"falló callado" es peor resultado que "no hay panel".

Ambas son chicas (≈6 fuentes × 365 días = 2.190 filas/año en runs) y sin FK,
así que `create_table` alcanza y no hay riesgo de lock largo.

Reversible sin pérdida operativa: lo que se pierde es cacheo recolectable de
nuevo desde las APIs dentro de su ventana, y `cost_snapshots` sobrevive.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d7c2f9a41b83"
# Re-parentada sobre `e4b8c2d6f0a1` (machine_transcription_evidence), que
# entró a staging mientras esto se desarrollaba. Las dos colgaban de
# `e3a7c9b1d5f0` y eso daba DOS cabezas: `alembic upgrade head` falla con
# "Multiple head revisions" y el release command de Railway corre
# exactamente ese comando, así que el deploy se habría caído.
#
# Se re-parenta en vez de agregar una migración de merge porque `cost_daily`
# no existe todavía en ningún entorno: nadie tiene esta revisión aplicada,
# así que mover el padre no deja ninguna base en un estado imposible.
down_revision: Union[str, Sequence[str], None] = "e4b8c2d6f0a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "cost_collection_runs",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False,
                  server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("day", "source"),
    )
    op.create_index("ix_cost_runs_status_day", "cost_collection_runs",
                    ["status", "day"])

    op.create_table(
        "cost_daily",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        # 'day' | 'month'. Un hecho mensual (suscripción plana, total de la
        # factura) NUNCA se puede sumar dentro de un rango de días; sin esta
        # columna en la PK, un rango que incluya el día 1 duplica el mes.
        sa.Column("grain", sa.String(length=8), nullable=False),
        sa.Column("dim_type", sa.String(length=16), nullable=False),
        sa.Column("dim_value", sa.String(length=255), nullable=False),
        sa.Column("qty", sa.Float(), nullable=True),
        sa.Column("unit", sa.String(length=32), nullable=True),
        # NULLABLE a propósito: una fuente que no se pudo consultar no puede
        # leerse como $0. Misma lección que `cost_snapshots.amount_usd`.
        sa.Column("amount_usd", sa.Float(), nullable=True),
        sa.Column("cost_behavior", sa.String(length=10), nullable=True),
        sa.Column("basis", sa.String(length=16), nullable=False,
                  server_default="measured"),
        sa.Column("basis_detail", sa.Text(), nullable=True),
        sa.Column("is_estimate", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("day", "source", "grain", "dim_type", "dim_value"),
    )
    op.create_index("ix_cost_daily_day_source", "cost_daily", ["day", "source"])
    op.create_index("ix_cost_daily_dim", "cost_daily", ["dim_type", "dim_value"])


def downgrade() -> None:
    op.drop_index("ix_cost_daily_dim", table_name="cost_daily")
    op.drop_index("ix_cost_daily_day_source", table_name="cost_daily")
    op.drop_table("cost_daily")
    op.drop_index("ix_cost_runs_status_day", table_name="cost_collection_runs")
    op.drop_table("cost_collection_runs")
