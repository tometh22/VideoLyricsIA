"""Expand the externally shared UMG portal DB without stamping its Alembic head.

Dry run by default. Run only with an explicit DELIVERIES_DATABASE_URL. The
staging job DB receives the full Alembic migration separately; the portal DB
needs only the delivery and change-request columns used by the new writer.
"""

from __future__ import annotations

import argparse
import os
import sys

from sqlalchemy import inspect, text

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from database import (  # noqa: E402
    DATABASE_URL, DELIVERIES_DATABASE_URL, deliveries_engine,
)


PORTAL_COLUMNS = {
    "deliveries": {
        "approved_revision": "INTEGER",
        "published_file_keys": "JSONB",
        "published_file_etags": "JSONB",
        "published_manifest_hash": "VARCHAR(64)",
        "retired_file_keys": "JSONB",
    },
    "delivery_change_requests": {
        "requested_revision": "INTEGER",
        "verified_render_fingerprint": "VARCHAR(64)",
        "verified_at": "TIMESTAMPTZ",
        "verified_by_user_id": "INTEGER",
        "verification_evidence": "JSONB",
    },
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Add missing columns")
    args = parser.parse_args()
    if not DELIVERIES_DATABASE_URL or deliveries_engine is None:
        parser.error("DELIVERIES_DATABASE_URL must point to the shared portal DB")
    if DELIVERIES_DATABASE_URL == DATABASE_URL:
        parser.error("Use Alembic for the local job DB, not this external DB script")
    if deliveries_engine.dialect.name != "postgresql":
        parser.error("Only PostgreSQL external portal databases are supported")

    inspector = inspect(deliveries_engine)
    missing = []
    for table, columns in PORTAL_COLUMNS.items():
        if not inspector.has_table(table):
            parser.error(f"Required portal table {table} is missing")
        existing = {row["name"] for row in inspector.get_columns(table)}
        missing.extend((table, name, kind) for name, kind in columns.items()
                       if name not in existing)
    for table, column, kind in missing:
        print(f"{table}.{column}: ADD {kind}")
    if not missing:
        print("Portal manifest schema already expanded")
        return 0
    if not args.apply:
        print(f"Dry run: {len(missing)} columns missing; pass --apply after release coordination")
        return 0

    with deliveries_engine.begin() as connection:
        connection.execute(text(
            "SELECT pg_advisory_xact_lock(hashtext('genly:portal-manifest-schema'))"
        ))
        for table, column, kind in missing:
            # Identifiers and SQL types are compile-time constants above.
            connection.execute(text(
                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {kind}"
            ))
    print(f"Applied portal manifest schema: {len(missing)} columns")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
