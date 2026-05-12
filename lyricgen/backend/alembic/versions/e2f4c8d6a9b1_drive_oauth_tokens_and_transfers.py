"""drive oauth tokens and transfers

Revision ID: e2f4c8d6a9b1
Revises: d3f7a1c09e52
Create Date: 2026-05-12 00:00:00.000000

Soporte para la integración Google Drive ("Guardar en Drive" button).
Dos tablas nuevas:

  user_drive_tokens
    OAuth refresh tokens (Fernet-encrypted) per user. Access tokens
    son short-lived y no se persisten — se derivan en cada uso.

  drive_transfers
    Track de una transferencia individual R2 → Drive disparada por el
    botón. El worker actualiza progress_pct mientras rclone corre.

CONCURRENTLY en CREATE INDEX porque las queries que usan estos
indexes corren en hot paths (Settings load, JobDetail load). En
build cold el índice es muy chico, no haría falta pero seguimos la
convención de las otras migraciones.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "e2f4c8d6a9b1"
down_revision: Union[str, Sequence[str], None] = "d3f7a1c09e52"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- user_drive_tokens ---
    op.create_table(
        "user_drive_tokens",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("encrypted_refresh_token", sa.String(length=2048), nullable=False),
        sa.Column("scope", sa.String(length=500), nullable=False),
        sa.Column("google_email", sa.String(length=255), nullable=True),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("user_id", name="uq_user_drive_tokens_user_id"),
    )
    op.create_index(
        "ix_user_drive_tokens_user_id",
        "user_drive_tokens",
        ["user_id"],
    )

    # --- drive_transfers ---
    op.create_table(
        "drive_transfers",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("job_id", sa.String(length=12), sa.ForeignKey("jobs.job_id"), nullable=False),
        sa.Column("file_type", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="queued"),
        sa.Column("progress_pct", sa.Integer(), nullable=True, server_default="0"),
        sa.Column("bytes_transferred", sa.BigInteger(), nullable=True, server_default="0"),
        sa.Column("bytes_total", sa.BigInteger(), nullable=True, server_default="0"),
        sa.Column("drive_file_id", sa.String(length=100), nullable=True),
        sa.Column("web_view_link", sa.String(length=500), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_drive_transfers_user_id",
        "drive_transfers",
        ["user_id"],
    )
    op.create_index(
        "ix_drive_transfers_job_id",
        "drive_transfers",
        ["job_id"],
    )
    op.create_index(
        "ix_drive_transfers_status",
        "drive_transfers",
        ["status"],
    )
    op.create_index(
        "ix_drive_transfers_created_at",
        "drive_transfers",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_drive_transfers_created_at", table_name="drive_transfers")
    op.drop_index("ix_drive_transfers_status", table_name="drive_transfers")
    op.drop_index("ix_drive_transfers_job_id", table_name="drive_transfers")
    op.drop_index("ix_drive_transfers_user_id", table_name="drive_transfers")
    op.drop_table("drive_transfers")

    op.drop_index("ix_user_drive_tokens_user_id", table_name="user_drive_tokens")
    op.drop_table("user_drive_tokens")
