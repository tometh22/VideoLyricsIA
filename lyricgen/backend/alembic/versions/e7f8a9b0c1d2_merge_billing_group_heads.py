"""merge heads: billing_group + (billing_status ⋈ telemetry)

Revision ID: e7f8a9b0c1d2
Revises: d1e2f3a4b5c6, b1c2d3e4f5a6
Create Date: 2026-06-02 20:00:00.000000

Punto de unión sin cambios de schema. En staging conviven tres líneas de
trabajo con migraciones que parten del mismo ancestro (b7d4e3a2c109):

  - c4d8e1f6a2b9  billing_status (sesión de billing)
  - a9b8c7d6e5f4  user_sessions + error_category (telemetría)
  - b1c2d3e4f5a6  billing_group (cuota compartida Universal)

d1e2f3a4b5c6 ya había unido las dos primeras; esta revisión une esa línea
con billing_group para que `alembic upgrade head` tenga UNA sola head y el
deploy de Railway no aborte con "Multiple head revisions are present".
"""
from __future__ import annotations

from typing import Sequence, Union


revision: str = "e7f8a9b0c1d2"
down_revision: Union[str, Sequence[str], None] = ("d1e2f3a4b5c6", "b1c2d3e4f5a6")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
