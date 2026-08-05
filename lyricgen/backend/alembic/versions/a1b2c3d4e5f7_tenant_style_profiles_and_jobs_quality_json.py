"""tenant_style_profiles + jobs.quality_json

Revision ID: a1b2c3d4e5f7
Revises: e5f6a7b8c9d0
Create Date: 2026-08-05 00:00:00.000000

Two independent additions that ship together because they answer the same
audit of UMG's 31 change requests (May–Aug 2026):

1. `tenant_style_profiles` — 9 of those 31 requests (29%) were the same two
   account preferences restated over and over ("sacar los puntos finales"
   × 6, "tipografía más grande" × 3) because nothing remembered them. The
   scope/scope_key pair lets one billing_group row cover all five Universal
   tenants. Seeded below for the accounts that actually asked.

2. `jobs.quality_json` — audio_coverage / text_mismatches / voiced_gaps were
   computed on every job and then discarded (the worker only persisted
   status, current_step and segments_json). Without them there is no way to
   gate a delivery on quality, and `coverage_warning` reads false forever on
   the async path. Nullable: old jobs simply never measured.

The seed is idempotent and best-effort — if a row already exists for a
scope it is left alone, so re-running never clobbers a profile an operator
edited by hand.
"""
from __future__ import annotations

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a1b2c3d4e5f7"
down_revision: Union[str, Sequence[str], None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Universal spans five tenants under one billing group. We seed the
# billing_group row (covers all of them, including accounts created later)
# AND the individual tenants, because `billing_group` is nullable on users
# and not every Universal user has it set today.
_UMG_TENANTS = (
    "umg", "omg", "umusic", "universal_argentina", "universal_chile",
)
_UMG_BILLING_GROUP = "universal_music"

# font_scale 1.3 is not a guess: of the 56 deliveries live on the portal,
# 24 were rendered at 1.3, 14 at 1.15 and 18 at 1.0 — the operator was
# picking it by hand on 38 of 56 and UMG still asked for "más grande"
# three times.
_UMG_PROFILE = {"strip_trailing_punctuation": True, "font_scale": 1.3}


def upgrade() -> None:
    op.create_table(
        "tenant_style_profiles",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("scope", sa.String(length=20), nullable=False),
        sa.Column("scope_key", sa.String(length=100), nullable=False),
        sa.Column("profile", sa.JSON(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=True,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True,
        ),
    )
    op.create_index(
        "ix_tenant_style_scope",
        "tenant_style_profiles",
        ["scope", "scope_key"],
        unique=True,
    )

    op.add_column("jobs", sa.Column("quality_json", sa.JSON(), nullable=True))

    # --- Seed the accounts that actually asked -------------------------
    conn = op.get_bind()
    rows = [{"scope": "billing_group", "scope_key": _UMG_BILLING_GROUP}]
    rows += [{"scope": "tenant", "scope_key": t} for t in _UMG_TENANTS]
    for row in rows:
        exists = conn.execute(
            sa.text(
                "SELECT 1 FROM tenant_style_profiles "
                "WHERE scope = :scope AND scope_key = :scope_key"
            ),
            row,
        ).fetchone()
        if exists:
            continue
        conn.execute(
            sa.text(
                "INSERT INTO tenant_style_profiles (scope, scope_key, profile) "
                "VALUES (:scope, :scope_key, :profile)"
            ),
            {**row, "profile": json.dumps(_UMG_PROFILE)},
        )


def downgrade() -> None:
    op.drop_column("jobs", "quality_json")
    op.drop_index("ix_tenant_style_scope", table_name="tenant_style_profiles")
    op.drop_table("tenant_style_profiles")
