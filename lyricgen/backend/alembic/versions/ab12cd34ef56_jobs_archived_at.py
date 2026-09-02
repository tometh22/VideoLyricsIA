"""jobs.archived_at — archivado de intentos fallidos (Fase 1)

Revision ID: ab12cd34ef56
Revises: f9a0b1c2d3e4
Create Date: 2026-06-10 11:30:00.000000

Cuando un job del mismo user+filename llega a `done`, los intentos
previos fallidos (error / rejected / validation_failed /
transcription_failed) se marcan con archived_at — nunca se borran
(audit trail UMG). La historia del cliente los esconde por default.

Index parcial-friendly: la query del archivador filtra por
(user_id, filename, status, archived_at IS NULL); el índice simple en
archived_at alcanza porque la historia filtra client-side y el
archivador ya usa el índice compuesto existente de (tenant, user).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "ab12cd34ef56"
down_revision: Union[str, Sequence[str], None] = "f9a0b1c2d3e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("jobs", "archived_at")
