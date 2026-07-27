"""merge heads (staging): config (c5d6e7f8a9b0) + billing-group merge (e7f8a9b0c1d2)

Revision ID: f9a0b1c2d3e4
Revises: c5d6e7f8a9b0, e7f8a9b0c1d2
Create Date: 2026-06-03 14:00:00.000000

SOLO STAGING. main tiene una cadena lineal (...→ b1c2d3e4f5a6 →
c5d6e7f8a9b0), pero staging arrastra una migración de merge extra
(e7f8a9b0c1d2, creada cuando se sincronizó billing+telemetry+billing_group
a staging). Al traer Config (c5d6e7f8a9b0, que parte de b1c2d3e4f5a6) a
staging quedan DOS heads → `alembic upgrade head` aborta con "Multiple
head revisions are present".

Esta revisión las une para que el deploy de staging tenga una sola head.
Sin cambios de schema. NO va a main (main no tiene e7f8a9b0c1d2).
"""
from __future__ import annotations

from typing import Sequence, Union


revision: str = "f9a0b1c2d3e4"
down_revision: Union[str, Sequence[str], None] = ("c5d6e7f8a9b0", "e7f8a9b0c1d2")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
