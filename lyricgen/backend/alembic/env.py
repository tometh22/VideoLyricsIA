"""Alembic environment.

Wires the migration framework to:
  • DATABASE_URL from the env (so prod/staging use Postgres without
    editing alembic.ini),
  • the SQLAlchemy Base from `database` so `--autogenerate` can
    diff models against the live schema.

Operator workflow (see docs/RUNBOOK_UMG.md for full guide):

    # one-time bootstrap on an EXISTING prod DB (current schema is the
    # baseline — don't actually run the initial migration's CREATE TABLEs):
    DATABASE_URL=... alembic stamp head

    # day-to-day after a model change:
    DATABASE_URL=sqlite:///dev.db alembic revision --autogenerate -m "add foo column"
    # review the generated file in alembic/versions/, edit if needed
    DATABASE_URL=... alembic upgrade head

CI runs `alembic upgrade head` against a fresh sqlite DB to verify
the migration chain is consistent.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# Make `import database` work regardless of where alembic is invoked
# from (the operator might run it from repo root or from backend/).
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from database import Base  # noqa: E402

config = context.config

# Pull DATABASE_URL from the env, override alembic.ini's placeholder.
# `init_db()` reads the same env var, so by definition migrations
# target the same DB the app will boot against.
_db_url = os.environ.get("DATABASE_URL", "").strip()
if _db_url.startswith("postgres://"):
    _db_url = _db_url.replace("postgres://", "postgresql://", 1)
if _db_url:
    config.set_main_option("sqlalchemy.url", _db_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Autogenerate diffs models vs DB.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout. Used when `alembic upgrade head --sql > x.sql`
    is preferred over running against a live connection (e.g. production
    DBA review)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live engine."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # compare_type catches column type changes (varchar → text)
            # that the default diff would miss.
            compare_type=True,
            # Render the schema name in CREATE TABLE so `alembic upgrade
            # head` works against shared Postgres clusters with named
            # schemas (Railway uses `public`; that's fine).
            include_schemas=False,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
