"""status page: incidentes públicos, timeline append-only y tramos de sonda

Revision ID: a1c3e5f7b902
Revises: f9a0b1c2d3e4, ba888d1665d8
Create Date: 2026-09-03 15:40:00.000000

Backs `status_page.py`: la página pública /status y la barra horizontal de
incidente en la home.

Tres tablas chicas y sin tráfico de escritura relevante:

  * `status_incidents` — una fila por incidente redactado a mano. Decenas
    por año, no miles.
  * `status_incident_updates` — el timeline. Append-only por diseño (ver el
    docstring del modelo). FK con ON DELETE CASCADE: borrar un incidente
    borra su relato, que es lo correcto — un timeline huérfano no significa
    nada.
  * `status_component_events` — UNA fila por transición de estado de un
    componente, con `last_seen_at` bumpeado en cada observación igual. Crece
    con los cambios de estado, no con el tráfico: unas pocas filas por mes
    por componente en régimen normal.

Ninguna tiene FK a `jobs` ni a `users` (solo `created_by` como string), así
que `create_table` alcanza y no hay riesgo de lock largo sobre tablas
calientes.

Reversible sin pérdida operativa: se pierde el historial de comunicación de
incidentes y las barras de 90 días. Nada del pipeline de generación depende
de estas tablas.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a1c3e5f7b902"
down_revision: Union[str, Sequence[str], None] = ("f9a0b1c2d3e4", "ba888d1665d8")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# `components` es JSONB en Postgres y JSON en SQLite (tests). Espeja el
# TypeDecorator JSONB de database.py, que hace exactamente este with_variant.
_JSONB = postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "status_incidents",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        # investigating | identified | monitoring | resolved
        sa.Column("status", sa.String(length=20), nullable=False,
                  server_default="investigating"),
        # none | minor | major | critical
        sa.Column("impact", sa.String(length=16), nullable=False,
                  server_default="minor"),
        sa.Column("components", _JSONB, nullable=True),
        # Editable a mano: el operador casi siempre se entera tarde, y el
        # historial de uptime usa esta ventana. Con created_at, todo
        # incidente reportado tarde se acortaría a sí mismo en el gráfico.
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("banner", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("public", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_status_incidents_started", "status_incidents", ["started_at"])
    op.create_index("ix_status_incidents_open", "status_incidents",
                    ["resolved_at", "public"])

    op.create_table(
        "status_incident_updates",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("incident_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["incident_id"], ["status_incidents.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_status_updates_incident", "status_incident_updates",
                    ["incident_id", "created_at"])

    op.create_table(
        "status_component_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("component", sa.String(length=32), nullable=False),
        # operational | degraded | partial_outage | major_outage
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.String(length=120), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        # Sin esto no se puede distinguir "verde 30 días" de "lo vimos verde
        # una vez hace 30 días". Un 100% de uptime fabricado por falta de
        # observaciones vuelve la página peor que no tenerla.
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_status_events_component_seen", "status_component_events",
                    ["component", "last_seen_at"])


def downgrade() -> None:
    op.drop_index("ix_status_events_component_seen",
                  table_name="status_component_events")
    op.drop_table("status_component_events")
    op.drop_index("ix_status_updates_incident", table_name="status_incident_updates")
    op.drop_table("status_incident_updates")
    op.drop_index("ix_status_incidents_open", table_name="status_incidents")
    op.drop_index("ix_status_incidents_started", table_name="status_incidents")
    op.drop_table("status_incidents")
