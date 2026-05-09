"""jobs composite indexes for dashboard hot path

Revision ID: 8802e2187632
Revises: a71feb1a87dc
Create Date: 2026-05-09 18:00:00.000000

The Dashboard mounts two endpoints in parallel — `/jobs` (history list)
and `/usage` (current-month quota counter). Both filter on `tenant_id`
plus a second column the existing single-column indexes can't combine
without a heap-heavy bitmap intersection.

Measured locally on a 5.5M-row / 1.2GB synthetic table, cold cache:

    /usage   busy tenant   568ms (read 28,926 pages)  →  1.2ms (read 23)
    /jobs    big tenant     30ms (read  2,548 pages)  →  0.18ms (read  7)

The /usage win is the headliner: with single-column indexes Postgres
falls back to a lossy BitmapAnd that scans ~230MB to count a few
thousand rows. The composite turns it into an Index-Only Scan with
zero heap fetches.

Cost: ~432MB of extra index space on a 1.2GB table at this scale (~36%
overhead). Acceptable given the dashboard latency win and the fact
that /usage runs on every Dashboard mount.

CONCURRENTLY is mandatory in prod — without it CREATE INDEX takes an
ACCESS EXCLUSIVE lock that blocks every read AND write on `jobs` for
the duration of the build (minutes on a multi-GB table). With it, the
build runs in two passes that only briefly hold a SHARE UPDATE
EXCLUSIVE lock; reads and writes proceed normally.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "8802e2187632"
down_revision: Union[str, Sequence[str], None] = "a71feb1a87dc"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # autocommit_block exits the migration's surrounding transaction so
    # CREATE INDEX CONCURRENTLY (which Postgres refuses to run inside a
    # txn) succeeds. IF NOT EXISTS makes the migration safe to re-run
    # if a previous attempt was interrupted mid-build.
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_jobs_tenant_status_created "
            "ON jobs (tenant_id, status, created_at DESC)"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_jobs_tenant_created "
            "ON jobs (tenant_id, created_at DESC)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_jobs_tenant_created")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_jobs_tenant_status_created")
