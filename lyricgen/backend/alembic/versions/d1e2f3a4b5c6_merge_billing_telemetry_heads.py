"""merge billing (c4d8e1f6a2b9) + telemetry (a9b8c7d6e5f4) heads

Revision ID: d1e2f3a4b5c6
Revises: a9b8c7d6e5f4, c4d8e1f6a2b9
Create Date: 2026-06-02 18:00:00.000000

Two features branched from b7d4e3a2c109 in parallel — Fase 2 billing
(c4d8e1f6a2b9, adds users.billing_status) and admin telemetry
(a9b8c7d6e5f4, user_sessions + job error_category). When both land on the
same branch (staging) Alembic sees two heads and `alembic upgrade head`
refuses to run. This is an empty merge revision that unifies them into a
single head — no schema change, just topology.

NOTE: this merge lives on the staging branch where BOTH parents exist. On
main, whichever of the two features merges SECOND will recreate the same
two-head split and will need an equivalent merge revision at that point.
"""
from __future__ import annotations

from typing import Sequence, Union


revision: str = "d1e2f3a4b5c6"
down_revision: Union[str, Sequence[str], None] = ("a9b8c7d6e5f4", "c4d8e1f6a2b9")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
