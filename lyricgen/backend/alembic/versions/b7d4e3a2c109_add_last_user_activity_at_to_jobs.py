"""add last_user_activity_at to jobs

Revision ID: b7d4e3a2c109
Revises: f1a2b3c4d5e6
Create Date: 2026-05-14 14:00:00.000000

Reaper anchor for "is this transcribed_pending session still alive?".
Before this column, find_abandoned_transcribed compared only created_at
against a fixed TTL — so a user who batch-edited 5 lyrics for 90 min
got reaped at the 30-min mark and lost everything (incident 2026-05-14,
Agus, 5 jobs deleted mid-batch).

After this column, the reaper uses coalesce(last_user_activity_at,
created_at). Any authenticated user touch (POST /save-segments, status
poll, etc) bumps the timestamp, so active sessions stay alive.

Nullable + no backfill: existing rows fall through to created_at via
coalesce, preserving current behavior. New touches start filling in.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "b7d4e3a2c109"
down_revision: Union[str, Sequence[str], None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column(
            "last_user_activity_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("jobs", "last_user_activity_at")
