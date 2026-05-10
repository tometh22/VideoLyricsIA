"""fix background_assets global visibility — revert overzealous backfill

Revision ID: d3f7a1c09e52
Revises: c91e3a4f2b18
Create Date: 2026-05-10

c91e3a4f2b18 backfilled owner_tenant_id = LIBRARY_OWNER_TENANT_ID (default
'universal-music') onto every pre-existing asset. The intended contract is
owner_tenant_id = NULL means "global / visible to everyone". Pre-existing
library assets are shared fallbacks, not tenant-exclusive content. This
migration resets them to NULL so non-UMG operators can see the library again.
"""
import os
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d3f7a1c09e52"
down_revision: Union[str, Sequence[str], None] = "c91e3a4f2b18"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    umg_tenant = os.environ.get("LIBRARY_OWNER_TENANT_ID", "universal-music")
    op.execute(
        sa.text(
            "UPDATE background_assets "
            "SET owner_tenant_id = NULL "
            "WHERE owner_tenant_id = :tenant"
        ).bindparams(tenant=umg_tenant)
    )


def downgrade() -> None:
    # Intentionally a no-op. Cannot safely distinguish assets that were
    # legitimately uploaded as global (NULL) after this fix from those
    # incorrectly backfilled by c91e3a4f2b18.
    pass
