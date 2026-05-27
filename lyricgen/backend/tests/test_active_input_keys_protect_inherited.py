"""Regression test for fix/audio-lost-variant-cleanup (2026-05-27).

Bug: storage._active_input_keys derived its protect-list from the job's
own `inputs/{tenant}/{job_id}/` prefix. When a variant inherited the
parent's input_r2_key (because /jobs/{id}/variant's copy_object had
failed silently), the cleanup cron didn't know to protect the parent's
key — so once the parent went terminal + 30 days, the WAV was deleted
while the live variant still pointed at it.

Two contract changes pinned here:

1. `_active_input_keys()` now returns `(prefixes, exact_keys)`. The
   exact_keys set MUST include the literal `job.input_r2_key` for every
   non-terminal job, even if it's outside the job's own prefix.

2. `cleanup_old_inputs()` must skip a key when EITHER:
     - it falls under a protected prefix (canonical case), OR
     - it matches a protected exact key (inherited case).
"""
import uuid
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def fresh_db(monkeypatch):
    """In-memory SQLite + fresh schema, isolated from any other test's state."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    import database
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", TestSession)
    database.Base.metadata.drop_all(bind=engine)
    database.Base.metadata.create_all(bind=engine)
    return TestSession


def _mk_job(db, *, job_id, tenant_id, status, input_r2_key):
    from database import Job
    j = Job(
        job_id=job_id,
        user_id=1,
        tenant_id=tenant_id,
        artist="x",
        song_title="y",
        style="oscuro",
        filename="track.wav",
        status=status,
        input_r2_key=input_r2_key,
    )
    db.add(j)
    db.commit()


def test_active_input_keys_returns_pair_of_sets(fresh_db):
    """Contract: returns (prefixes, exact_keys) — both sets, never None."""
    import storage
    prefixes, exact_keys = storage._active_input_keys()
    assert isinstance(prefixes, set)
    assert isinstance(exact_keys, set)


def test_exact_keys_includes_inherited_parent_key(fresh_db):
    """Variant inherits parent's key (copy_object had failed pre-fix). The
    protect-list MUST include that exact key so cleanup_old_inputs doesn't
    delete the WAV the live variant is still reading from."""
    import storage
    db = fresh_db()
    try:
        # Parent: terminal (done), key under its own prefix.
        _mk_job(db, job_id="parent11111", tenant_id="t1",
                status="done", input_r2_key="inputs/t1/parent11111/track.wav")
        # Variant: still processing, INHERITED parent's key.
        _mk_job(db, job_id="variant22222", tenant_id="t1",
                status="processing", input_r2_key="inputs/t1/parent11111/track.wav")
    finally:
        db.close()

    prefixes, exact_keys = storage._active_input_keys()
    # Variant is non-terminal → its OWN prefix is in `prefixes`.
    assert "inputs/t1/variant22222/" in prefixes
    # Critical: variant's input_r2_key (pointing at parent's WAV) is
    # listed in `exact_keys`, even though it doesn't fall under
    # variant's own prefix.
    assert "inputs/t1/parent11111/track.wav" in exact_keys


def test_cleanup_old_inputs_skips_inherited_key(fresh_db, monkeypatch):
    """End-to-end: simulate R2 list_objects returning the parent's old
    WAV (modified > retention). cleanup_old_inputs must skip it because
    variant still references it via input_r2_key, NOT delete it."""
    import storage
    db = fresh_db()
    try:
        _mk_job(db, job_id="parentXX", tenant_id="t1",
                status="done", input_r2_key="inputs/t1/parentXX/old.wav")
        _mk_job(db, job_id="variantYY", tenant_id="t1",
                status="processing", input_r2_key="inputs/t1/parentXX/old.wav")
    finally:
        db.close()

    # Stub the R2 client to return one expired object that matches the
    # inherited key.
    from datetime import datetime, timezone, timedelta
    fake_old = datetime.now(timezone.utc) - timedelta(days=60)

    fake_paginator = MagicMock()
    fake_paginator.paginate.return_value = [{
        "Contents": [{
            "Key": "inputs/t1/parentXX/old.wav",
            "Size": 50 * 1024 * 1024,
            "LastModified": fake_old,
        }],
    }]
    fake_client = MagicMock()
    fake_client.get_paginator.return_value = fake_paginator
    # delete_objects must NOT be called.
    fake_client.delete_objects = MagicMock()

    monkeypatch.setattr(storage, "_get_client", lambda: fake_client)
    monkeypatch.setattr(storage, "R2_BUCKET", "test-bucket")

    report = storage.cleanup_old_inputs(retention_days=30, apply=True)

    # Found 1 expired key, but it was protected → skipped, not deleted.
    assert report["scanned"] == 1
    assert report["expired"] == 0  # because the protect-list filtered it
    assert report["skipped_active"] == 1
    assert report["deleted"] == 0
    fake_client.delete_objects.assert_not_called()
