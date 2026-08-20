"""Tests for the three prod-readiness fixes:

  • Veo retry backoff includes jitter (no thundering herd).
  • Reaper acquires a Postgres advisory lock so multi-replica API is safe.
  • Alembic upgrade head creates the full schema from scratch.
"""

import os
import subprocess
import tempfile
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Veo retry jitter
# ---------------------------------------------------------------------------


def test_veo_safe_retry_backoff_includes_jitter():
    """Locked-in: the 429-retry sleep must vary per call. Without
    jitter, N parallel jobs that hit a 429 at the same instant retry
    in lock-step → second wave of 429s → cascade. We verify by
    inspecting `pipeline.py` source for the jitter pattern AND
    independently confirming the formula yields jittered values across
    repeated invocations.

    Why source inspection: `_generate_veo_video` lazily imports
    requests / time inside the function body, which makes
    monkey-patching brittle (the imports happen on each call).
    Asserting the source uses random.uniform(...) on the wait is the
    most reliable way to lock in the regression.
    """
    import pipeline as _pipeline
    import inspect
    import random

    src = inspect.getsource(_pipeline._generate_veo_video)
    # Only an explicit 429 is safe to retry: a network timeout may arrive
    # after Vertex accepted the operation, so retrying it can double-charge.
    assert src.count("random.uniform") >= 1, (
        "Expected jitter on the confirmed-429 retry path."
    )
    assert "ambiguous submission; not retrying" in src
    assert "VeoAmbiguousSubmission" in src
    assert "0.8" in src and "1.2" in src, (
        "Jitter window expected to be ±20 % (0.8-1.2 multiplier)."
    )

    # Independent sanity: the formula `base * random.uniform(0.8, 1.2)`
    # produces non-identical values across N calls.
    random.seed(42)
    base = 30.0
    samples = [base * random.uniform(0.8, 1.2) for _ in range(10)]
    assert len(set(round(s, 3) for s in samples)) > 5, (
        f"Jitter formula not actually random: {samples}"
    )
    assert all(24.0 <= s <= 36.0 for s in samples), (
        f"Jitter samples out of expected ±20 % window: {samples}"
    )


# ---------------------------------------------------------------------------
# Reaper advisory lock
# ---------------------------------------------------------------------------


def test_reaper_skips_when_advisory_lock_unavailable(monkeypatch):
    """When pg_try_advisory_lock returns false (another replica holds
    it), reap_all_stuck must short-circuit and return 0. Without this
    guard, every replica reaps in parallel and triplicates the DB
    load + notification noise."""
    import reaper as _reaper

    # Build a fake session that:
    #   - reports postgresql dialect,
    #   - returns False from pg_try_advisory_lock,
    #   - tracks whether find_stuck_jobs was called.
    fake_db = MagicMock()
    fake_db.bind.dialect.name = "postgresql"
    fake_lock_result = MagicMock()
    fake_lock_result.scalar.return_value = False  # lock NOT acquired
    fake_db.execute.return_value = fake_lock_result

    monkeypatch.setattr(_reaper, "SessionLocal", lambda: fake_db)
    find_called = []
    monkeypatch.setattr(
        _reaper, "find_stuck_jobs",
        lambda *a, **kw: find_called.append(True) or [],
    )

    n = _reaper.reap_all_stuck()
    assert n == 0
    assert not find_called, (
        "reaper should short-circuit before scanning when lock is held"
    )
    fake_db.close.assert_called()


def test_reaper_runs_when_advisory_lock_acquired(monkeypatch):
    """Happy path — lock acquired, scan runs, no jobs to reap."""
    import reaper as _reaper

    fake_db = MagicMock()
    fake_db.bind.dialect.name = "postgresql"
    fake_lock_result = MagicMock()
    fake_lock_result.scalar.return_value = True  # lock acquired
    fake_db.execute.return_value = fake_lock_result

    monkeypatch.setattr(_reaper, "SessionLocal", lambda: fake_db)
    monkeypatch.setattr(_reaper, "find_stuck_jobs", lambda *a, **kw: [])

    n = _reaper.reap_all_stuck()
    assert n == 0
    # Verify both lock + unlock were called by inspecting the
    # TextClause SQL string of each db.execute(text(...)) call.
    sql_texts = [
        c.args[0].text for c in fake_db.execute.call_args_list
        if c.args and hasattr(c.args[0], "text")
    ]
    assert any("pg_try_advisory_lock" in s for s in sql_texts), sql_texts
    assert any("pg_advisory_unlock" in s for s in sql_texts), sql_texts


def test_reaper_skips_advisory_lock_on_sqlite(monkeypatch):
    """Tests run against sqlite, which doesn't have advisory locks.
    Reaper must proceed without calling them — otherwise the test
    suite breaks every time someone touches the reaper."""
    import reaper as _reaper

    fake_db = MagicMock()
    fake_db.bind.dialect.name = "sqlite"

    monkeypatch.setattr(_reaper, "SessionLocal", lambda: fake_db)
    monkeypatch.setattr(_reaper, "find_stuck_jobs", lambda *a, **kw: [])

    n = _reaper.reap_all_stuck()
    assert n == 0
    # No advisory lock call on sqlite.
    sql_texts = [
        c.args[0].text for c in fake_db.execute.call_args_list
        if c.args and hasattr(c.args[0], "text")
    ]
    assert not any("advisory" in s for s in sql_texts), sql_texts


# ---------------------------------------------------------------------------
# Alembic — full schema bootstrap from scratch
# ---------------------------------------------------------------------------


def test_alembic_upgrade_head_creates_full_schema(tmp_path):
    """`alembic upgrade head` against an empty DB must create every
    table the SQLAlchemy models declare. Catches drift between
    `database.py` and the migration chain."""
    db_path = tmp_path / "alembic_test.db"
    db_url = f"sqlite:///{db_path}"

    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    env = {
        **os.environ,
        "DATABASE_URL": db_url,
        "JWT_SECRET": "test",
        "ADMIN_PASSWORD": "test123ab",
        "ENVIRONMENT": "development",
    }

    result = subprocess.run(
        ["alembic", "upgrade", "head"],
        cwd=backend_dir, env=env,
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, (
        f"alembic upgrade failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )

    # Verify every model's table now exists.
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    tables = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    conn.close()

    # Every SQLAlchemy model must be present. If you add a new table
    # without an alembic revision, this test fails.
    expected = {
        "users", "jobs", "invoices", "audit_log",
        "background_assets", "ai_provenance",
        "veo_budget_ledger",
        "password_reset_tokens", "email_verification_tokens",
        "user_settings", "lyrics_cache",
        "alembic_version",
    }
    missing = expected - tables
    assert not missing, f"Migration missed tables: {missing}\nFound: {tables}"

    conn = sqlite3.connect(str(db_path))
    provenance_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(ai_provenance)")
    }
    provenance_indexes = {
        row[1] for row in conn.execute("PRAGMA index_list(ai_provenance)")
    }
    ledger_indexes = {
        row[1] for row in conn.execute("PRAGMA index_list(veo_budget_ledger)")
    }
    conn.close()

    assert "reservation_expires_at" in provenance_columns
    assert "ix_ai_provenance_reservation_expires_at" in provenance_indexes
    assert "ix_veo_budget_ledger_scope_call_at" in ledger_indexes
    assert "ix_veo_budget_ledger_archived_at" in ledger_indexes


def test_cost_controls_upgrade_from_observability_head(tmp_path):
    """The staged rollout applies the additive schema in #1084 before the
    # #1085 workers use it. Exercise that exact a1 -> head upgrade, not only a
    # fresh database bootstrap."""
    import importlib.util

    try:
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
    except ImportError:
        pytest.skip("Alembic CLI/runtime is installed in CI and production")

    from sqlalchemy import Column, Integer, MetaData, Table, create_engine, inspect

    db_path = tmp_path / "observability_to_controls.db"
    upgrade_engine = create_engine(f"sqlite:///{db_path}")
    # Model the exact pre-control surface instead of calling current
    # Base.metadata.create_all(): on #1085 the current model already contains
    # the new table/column, which would make this upgrade test a false failure.
    pre_controls = MetaData()
    Table(
        "ai_provenance",
        pre_controls,
        Column("id", Integer, primary_key=True),
    )
    pre_controls.create_all(upgrade_engine)
    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    versions_dir = os.path.join(backend_dir, "alembic", "versions")

    with upgrade_engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        for filename in (
            "b2c3d4e5f6a7_veo_budget_ledger.py",
            "c3d4e5f6a7b8_veo_reservation_lease.py",
        ):
            path = os.path.join(versions_dir, filename)
            spec = importlib.util.spec_from_file_location(filename, path)
            module = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(module)
            module.op = operations
            module.upgrade()

    schema = inspect(upgrade_engine)
    assert "veo_budget_ledger" in schema.get_table_names()
    assert "reservation_expires_at" in {
        column["name"] for column in schema.get_columns("ai_provenance")
    }
    assert {
        "ix_veo_budget_ledger_scope_call_at",
        "ix_veo_budget_ledger_archived_at",
    } <= {index["name"] for index in schema.get_indexes("veo_budget_ledger")}
    assert "ix_ai_provenance_reservation_expires_at" in {
        index["name"] for index in schema.get_indexes("ai_provenance")
    }


def test_alembic_current_matches_head_after_upgrade(tmp_path):
    """After `upgrade head`, `alembic current` should report the same
    revision as the head of the migration chain. If not, the chain is
    broken (forked, missing revision)."""
    db_path = tmp_path / "alembic_current.db"
    db_url = f"sqlite:///{db_path}"

    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    env = {
        **os.environ,
        "DATABASE_URL": db_url,
        "JWT_SECRET": "test",
        "ADMIN_PASSWORD": "test123ab",
        "ENVIRONMENT": "development",
    }

    subprocess.run(
        ["alembic", "upgrade", "head"],
        cwd=backend_dir, env=env, check=True,
        capture_output=True, timeout=60,
    )
    head_proc = subprocess.run(
        ["alembic", "heads"],
        cwd=backend_dir, env=env, check=True,
        capture_output=True, text=True, timeout=15,
    )
    current_proc = subprocess.run(
        ["alembic", "current"],
        cwd=backend_dir, env=env, check=True,
        capture_output=True, text=True, timeout=15,
    )
    # First token of each output is the revision id.
    head_rev = head_proc.stdout.strip().split()[0]
    current_rev = current_proc.stdout.strip().split()[0]
    assert head_rev == current_rev, (
        f"current ({current_rev}) drifts from head ({head_rev}) — "
        "migration chain is broken"
    )
