"""background_assets tenant scope and asset_usage tracking

Revision ID: c91e3a4f2b18
Revises: 8802e2187632
Create Date: 2026-05-09 19:00:00.000000

Universal Music (our first paying tenant) requires that the videos and
backgrounds in their library are exclusive to them — no other tenant
should be able to see or generate from those assets. Until now the
library was global to every authenticated user.

Schema changes:
  * background_assets.owner_tenant_id (nullable string) — NULL means
    "global / visible to everyone" (kept for the eventual public library
    in phase 2). A tenant_id string locks the asset to that tenant.
  * background_assets.parent_asset_id (nullable FK self) — when an asset
    was created as a variation derived from another library asset
    (image-to-video on a frame of the parent), this points to the
    original. Used for audit and "derived from X" UI.
  * asset_usage table — one row per (tenant, library asset, generation
    job). Backs the "you already used this background on [date]"
    warning in the picker and gives UMG the audit trail they asked for.

Data migration:
  Every existing background_asset is reassigned to UMG's tenant_id. The
  value is taken from the LIBRARY_OWNER_TENANT_ID env var (defaults to
  "universal-music") so we can run the migration with the actual
  tenant_id chosen when the UMG user is provisioned.
"""
import os
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c91e3a4f2b18"
down_revision: Union[str, Sequence[str], None] = "8802e2187632"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "background_assets",
        sa.Column("owner_tenant_id", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "background_assets",
        sa.Column("parent_asset_id", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_background_assets_owner_tenant_id",
        "background_assets",
        ["owner_tenant_id"],
        unique=False,
    )
    if op.get_context().dialect.name == "postgresql":
        op.create_foreign_key(
            "fk_background_assets_parent_asset_id",
            "background_assets",
            "background_assets",
            ["parent_asset_id"],
            ["id"],
        )

    # Reassign existing assets to UMG. The tenant_id used here must match
    # the tenant_id of the UMG user when it gets created. Override via
    # LIBRARY_OWNER_TENANT_ID at migration time if needed.
    umg_tenant = os.environ.get("LIBRARY_OWNER_TENANT_ID", "universal-music")
    op.execute(
        sa.text(
            "UPDATE background_assets SET owner_tenant_id = :tenant "
            "WHERE owner_tenant_id IS NULL"
        ).bindparams(tenant=umg_tenant)
    )

    op.create_table(
        "asset_usage",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("asset_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.String(length=100), nullable=False),
        sa.Column("job_id", sa.String(length=12), nullable=True),
        sa.Column("mode", sa.String(length=20), nullable=False, server_default="as_is"),
        sa.Column(
            "used_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(["asset_id"], ["background_assets.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_asset_usage_asset_id", "asset_usage", ["asset_id"], unique=False)
    op.create_index("ix_asset_usage_tenant_id", "asset_usage", ["tenant_id"], unique=False)
    op.create_index("ix_asset_usage_job_id", "asset_usage", ["job_id"], unique=False)
    op.create_index("ix_asset_usage_used_at", "asset_usage", ["used_at"], unique=False)
    op.create_index(
        "ix_asset_usage_asset_tenant",
        "asset_usage",
        ["asset_id", "tenant_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_asset_usage_asset_tenant", table_name="asset_usage")
    op.drop_index("ix_asset_usage_used_at", table_name="asset_usage")
    op.drop_index("ix_asset_usage_job_id", table_name="asset_usage")
    op.drop_index("ix_asset_usage_tenant_id", table_name="asset_usage")
    op.drop_index("ix_asset_usage_asset_id", table_name="asset_usage")
    op.drop_table("asset_usage")

    if op.get_context().dialect.name == "postgresql":
        op.drop_constraint(
            "fk_background_assets_parent_asset_id",
            "background_assets",
            type_="foreignkey",
        )
    op.drop_index(
        "ix_background_assets_owner_tenant_id",
        table_name="background_assets",
    )
    op.drop_column("background_assets", "parent_asset_id")
    op.drop_column("background_assets", "owner_tenant_id")
