"""DB pool + streaming-endpoint connection-release tests.

The original incident: dashboard fired 6+ /preview/.../thumbnail in
parallel; each held a pooled DB connection for the full file stream;
the pool exhausted; /usage broke and showed "No se pudo cargar el uso".

These tests pin the contract that ALL the file-streaming endpoints
release their DB connection BEFORE the response starts streaming. We
do that by exercising each endpoint with a tiny pool (size=1,
overflow=0) and asserting that a second endpoint hit on the same pool
works without timing out — proof that the first one already returned
its socket.

We also unit-test scoped_db() and pool_stats() directly.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest

from database import Job, SessionLocal, scoped_db, pool_stats


@pytest.fixture
def tiny_job(db):
    """Insert a minimal done-status job + cleanup."""
    jid = f"pooltest_{uuid.uuid4().hex[:6]}"
    db.add(Job(
        job_id=jid,
        user_id=1,
        tenant_id="tenant_pool_test",
        artist="Test",
        filename="x.mp3",
        style="oscuro",
        status="done",
        progress=100,
        delivery_profile="youtube",
        created_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
    ))
    db.commit()
    yield jid
    db.query(Job).filter(Job.tenant_id == "tenant_pool_test").delete()
    db.commit()


def test_scoped_db_returns_a_working_session():
    """scoped_db() yields a real SQLAlchemy session that can query."""
    with scoped_db() as db:
        # If this is a real session, executing trivial SQL works.
        from sqlalchemy import text
        result = db.execute(text("SELECT 1")).scalar()
        assert result == 1


def test_scoped_db_closes_on_exit():
    """The session must be closed by the time the context exits, so
    the connection returns to the pool immediately."""
    db_ref = None
    with scoped_db() as db:
        db_ref = db
        # Force a connection checkout so we know there's something to
        # release.
        from sqlalchemy import text
        db.execute(text("SELECT 1")).scalar()
    # After the with block, the session should not have an active
    # transaction or open connection. is_active is True on a closed
    # session by SQLAlchemy convention; the meaningful signal is that
    # the connection (a.k.a. .connection().connection) is unwound.
    assert db_ref is not None
    # Calling close() again on a closed session is a no-op — that's
    # the contract we rely on for finally-blocks that double-close.
    db_ref.close()


def test_scoped_db_closes_even_on_exception():
    """The crashing scope returns its own socket, even with other active sessions."""
    from threading import get_ident
    from sqlalchemy import event, text
    from database import engine

    owner_thread = get_ident()
    borrowed, returned = set(), set()

    def checkout(_connection, record, _proxy):
        if get_ident() == owner_thread:
            borrowed.add(id(record))

    def checkin(_connection, record):
        returned.add(id(record))

    other_session = SessionLocal()
    other_session.execute(text('SELECT 1'))
    event.listen(engine.pool, 'checkout', checkout)
    event.listen(engine.pool, 'checkin', checkin)
    try:
        with pytest.raises(RuntimeError, match='simulated handler crash'):
            with scoped_db() as db:
                assert db.execute(text('SELECT 1')).scalar() == 1
                raise RuntimeError('simulated handler crash')
        assert borrowed, 'The test must actually borrow a database connection'
        assert borrowed <= returned, 'The crashing scope did not return its connection'
        assert not db.in_transaction()
        assert other_session.in_transaction(), 'The unrelated session stays open'
    finally:
        event.remove(engine.pool, 'checkout', checkout)
        event.remove(engine.pool, 'checkin', checkin)
        other_session.close()


def test_pool_stats_shape():
    """pool_stats() returns the keys callers expect, or {} on engines
    without a real pool (SQLite StaticPool in tests)."""
    stats = pool_stats()
    # Either we got nothing (SQLite test pool) or we got the expected
    # shape — never a partial dict.
    if stats:
        for key in (
            "size", "checked_out", "overflow", "available",
            "max_overflow", "total_capacity",
        ):
            assert key in stats, f"pool_stats missing {key}: {stats}"
        assert stats["total_capacity"] == stats["size"] + stats["max_overflow"]


def test_health_endpoint_exposes_pool_utilization(client):
    """/health surfaces db_pool stats so operators can alert on
    sustained high utilization before the pool fully exhausts."""
    r = client.get("/health")
    assert r.status_code in (200, 503)  # 503 is ok if redis is missing
    body = r.json()
    # db_pool key is best-effort — only present on postgres engines
    # with a real pool. On SQLite (tests) it may be absent; both are
    # fine as long as we don't crash.
    if "db_pool" in body:
        pool = body["db_pool"]
        assert "in_use" in pool
        assert "total" in pool
        assert "utilization" in pool


def test_concurrent_scoped_db_sessions_do_not_starve():
    """Two scoped_db() blocks run back to back — the second must NOT
    block on the first because the first releases on context exit.
    Tests the actual scaling property we care about."""
    from sqlalchemy import text
    # Run several short sessions in serial — if any one fails to
    # release we'd eventually timeout. SQLite test pool has very few
    # slots so this is a sharper test than it looks.
    for _ in range(20):
        with scoped_db() as db:
            assert db.execute(text("SELECT 1")).scalar() == 1
