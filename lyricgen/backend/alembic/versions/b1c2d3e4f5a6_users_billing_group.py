"""users.billing_group — cuota compartida entre tenants

Revision ID: b1c2d3e4f5a6
Revises: a9b8c7d6e5f4
Create Date: 2026-06-02 19:00:00.000000

Caso Universal Music: dos tenants separados (universal_argentina y
universal_chile, sin visibilidad cruzada de videos) que comparten UN solo
plan de 250 videos/mes como cuenta. Los usuarios de ambos tenants llevan
billing_group="universal_music" y auth.get_plan_usage() cuenta la cuota
sobre todos los tenants del grupo.

NULL = sin grupo → cuota por tenant (comportamiento histórico, sin cambios
para los usuarios existentes).

Additive y lock-light (ADD COLUMN nullable, sin default) — segura en
rolling deploy.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "b1c2d3e4f5a6"
down_revision: Union[str, Sequence[str], None] = "a9b8c7d6e5f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("billing_group", sa.String(length=100), nullable=True),
    )
    op.create_index("ix_users_billing_group", "users", ["billing_group"])


def downgrade() -> None:
    op.drop_index("ix_users_billing_group", table_name="users")
    op.drop_column("users", "billing_group")
