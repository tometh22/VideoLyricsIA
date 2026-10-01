"""provenance predict and queue time

ai_provenance: desglosar duration_ms en cola vs inferencia.

Incidente 2026-08-26/28 (UMG Chile): `duration_ms` mide la ventana entera de
`replicate.run`/poll, así que una corrida lenta era indistinguible entre "el
modelo tardó" y "esperamos GPU" — dos problemas con soluciones opuestas.
Diagnosticarlo obligó a ir a la API de Replicate a mano.

El desglose (238 corridas de demucs, jun-ago 2026) mostró que la degradación
fue 100% cola: `predict_time` mediana 87,6 s ANTES y DESPUÉS, con la cola
pasando de 23,5 s a 204,3 s (picos de 1824 s). Sin estas dos columnas esa
conclusión no se puede sacar desde nuestra propia telemetría.

Revision ID: ba888d1665d8
Revises: e5a7c9b1d3f6 (en `main` revisaba c8d9e0f1a2b3)
Create Date: 2026-08-29 19:09:40.198113

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ba888d1665d8'
down_revision: Union[str, Sequence[str], None] = 'e5a7c9b1d3f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Nullable a propósito: las filas históricas no tienen el dato, y los
    # proveedores que no exponen el desglose (OpenAI, Vertex) lo dejan en NULL.
    # Idempotente: esta revisión nació en `main` (cadena distinta) y se
    # re-encadenó detrás de e5a7c9b1d3f6 al alinear las líneas; una base que
    # ya la hubiera corrido no debe romper el deploy.
    if op.get_context().dialect.name == "postgresql":
        for column in ("predict_time_ms", "queue_time_ms"):
            op.execute(f"ALTER TABLE ai_provenance ADD COLUMN IF NOT EXISTS {column} INTEGER")
        return
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("ai_provenance")}
    for column in ("predict_time_ms", "queue_time_ms"):
        if column not in existing:
            op.add_column("ai_provenance", sa.Column(column, sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("ai_provenance", "queue_time_ms")
    op.drop_column("ai_provenance", "predict_time_ms")
