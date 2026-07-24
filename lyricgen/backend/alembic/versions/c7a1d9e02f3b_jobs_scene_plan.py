"""jobs.scene_plan — add-on premium "Escenas" (multi-escena)

Revision ID: c7a1d9e02f3b
Revises: 1e9b7dc0edb8
Create Date: 2026-06-17 12:00:00.000000

Storyboard generado por scenes.build_scene_plan (biblia visual + secciones +
escenas con recurrencia de coro). NULL = job de fondo único (camino
histórico). El toggle de opt-in del usuario vive en render_params
("enable_scenes": true), que ya es JSONB — por eso aquí sólo agregamos el
storyboard. JSONB nullable, sin default: las filas viejas quedan en NULL y el
render cae al loop único de siempre (cero regresión).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c7a1d9e02f3b"
down_revision: Union[str, Sequence[str], None] = "1e9b7dc0edb8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("scene_plan", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("jobs", "scene_plan")
