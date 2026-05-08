#!/usr/bin/env bash
# Prod migration runner — invoked by the Railway "release" command before
# the new image starts serving. Idempotent: applies any pending Alembic
# revisions, no-ops when the DB is already at head.
#
# Required env: DATABASE_URL.
#
# Failure modes:
#   • Pending migration that the new image doesn't ship → exit 1, deploy
#     aborts, old image keeps serving (Railway behaviour).
#   • Postgres unreachable → exit 1, deploy aborts.
#
# First-time bootstrap on an existing prod DB (one-shot, manual):
#
#     DATABASE_URL="<prod url>" alembic stamp head
#
# That marks the live schema as already-migrated without running the
# initial CREATE TABLEs. Subsequent deploys then run `upgrade head`
# safely. If you forget the stamp, `upgrade head` will try to CREATE
# TABLE on tables that already exist and fail loudly — recoverable, but
# noisy.

set -euo pipefail

if [ -z "${DATABASE_URL:-}" ]; then
    echo "ERROR: DATABASE_URL must be set" >&2
    exit 1
fi

cd "$(dirname "$0")/.."  # cd to backend/

echo "[migrate] alembic upgrade head"
alembic upgrade head
echo "[migrate] done"
