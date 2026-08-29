"""ai_provenance: desglosar duration_ms en cola vs inferencia

Incidente 2026-08-26/28 (UMG Chile): `duration_ms` mide la ventana entera de
`replicate.run`/poll, así que una corrida lenta era indistinguible entre "el
modelo tardó" y "esperamos GPU". Diagnosticarlo obligó a ir a la API de
Replicate a mano.

El desglose (medido sobre 238 corridas de demucs, jun-ago 2026) mostró que la
degradación fue 100% cola: `predict_time` mediana 87,6s ANTES y DESPUÉS, con
la cola pasando de 23,5s a 204,3s (picos de 1824s). Sin estas dos columnas esa
conclusión no se puede sacar desde nuestra propia telemetría.

Revision ID: d7e9c2a4f1b8
Revises: c8d9e0f1a2b3
Create Date: 2026-08-29
"""
from alembic import op
import sqlalchemy as sa

revision = "d7e9c2a4f1b8"
down_revision = "c8d9e0f1a2b3"
branch_labels = None
depends_on = None


def upgrade():
    # Nullable a propósito: las filas históricas no tienen el dato, y los
    # proveedores que no exponen el desglose (OpenAI, Vertex) lo dejan en NULL.
    op.add_column("ai_provenance",
                  sa.Column("predict_time_ms", sa.Integer(), nullable=True))
    op.add_column("ai_provenance",
                  sa.Column("queue_time_ms", sa.Integer(), nullable=True))


def downgrade():
    op.drop_column("ai_provenance", "queue_time_ms")
    op.drop_column("ai_provenance", "predict_time_ms")
