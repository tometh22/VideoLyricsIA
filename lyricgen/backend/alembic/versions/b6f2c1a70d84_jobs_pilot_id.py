"""jobs.pilot_id — copia de prueba del piloto de revisor

Revision ID: b6f2c1a70d84
Revises: a1c3e5f7b902
Create Date: 2026-09-08 02:10:00.000000

NULL en todos los jobs existentes y en cualquier upload normal. No nulo
significa, y el servidor lo hace cumplir, que la fila es una copia de prueba
de una corrida del piloto: escribible sólo por su usuario de agente
(editor.save_document), no aprobable, y rechazada por /generate, /retry y
/jobs/{id}/variant.

Sin índice a propósito: ningún camino de lectura filtra por esta columna, se
consulta siempre por job_id. `ADD COLUMN` nullable sin default es una
operación de sólo metadata en PostgreSQL, no reescribe la tabla.

Esta revisión existe porque el contrato del repositorio es que el esquema lo
instala Alembic antes de que arranque el código: `railway/api.toml` corre
`prod_migrate.sh` como preDeployCommand y `require_api_schema.py` se niega a
arrancar si al modelo le falta una columna. La lista `column_adds` de
`database.py` es sólo auto-reparación heredada y se ejecuta demasiado tarde.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b6f2c1a70d84"
down_revision: Union[str, Sequence[str], None] = "a1c3e5f7b902"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("pilot_id", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("jobs", "pilot_id")
