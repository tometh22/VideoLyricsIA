"""perfil (full_name, avatar_url) + login_sessions

Revision ID: c5d6e7f8a9b0
Revises: b1c2d3e4f5a6
Create Date: 2026-06-03 12:00:00.000000

Configuración nivel SaaS (sin 2FA por ahora):
- users.full_name, users.avatar_url → perfil de usuario (nombre + avatar)
- login_sessions → "Configuración → Dispositivos": una fila por sesión de
  login (jti del JWT, ip, user_agent), para listar dispositivos y cerrar
  sesión remota (revoked_at).

Additive y lock-light. login_sessions arranca vacía; los tokens activos
hoy no tienen jti → get_current_user los acepta sin chequear sesión
(grandfather) y expiran solos.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "c5d6e7f8a9b0"
down_revision: Union[str, Sequence[str], None] = "b1c2d3e4f5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("full_name", sa.String(length=200), nullable=True))
    op.add_column("users", sa.Column("avatar_url", sa.String(length=500), nullable=True))

    op.create_table(
        "login_sessions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("jti", sa.String(length=64), nullable=False),
        sa.Column("ip_address", sa.String(length=45), nullable=True),
        sa.Column("user_agent", sa.String(length=400), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("jti"),
    )
    op.create_index("ix_login_sessions_user_id", "login_sessions", ["user_id"])
    op.create_index("ix_login_sessions_jti", "login_sessions", ["jti"])
    op.create_index("ix_login_sessions_user_active", "login_sessions", ["user_id", "revoked_at"])


def downgrade() -> None:
    op.drop_index("ix_login_sessions_user_active", table_name="login_sessions")
    op.drop_index("ix_login_sessions_jti", table_name="login_sessions")
    op.drop_index("ix_login_sessions_user_id", table_name="login_sessions")
    op.drop_table("login_sessions")
    op.drop_column("users", "avatar_url")
    op.drop_column("users", "full_name")
