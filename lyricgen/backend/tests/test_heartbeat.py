"""U10 (audit Sprint 2 2026-05-25) — jobs.heartbeat() tests.

Heartbeat: bump Job.last_progress_at = NOW() sin tocar otros campos.
Usado por workers en steps largos (Veo polling) para que el reaper
no los mate por aged-anchor.
"""

import time
from datetime import datetime, timedelta, timezone

import pytest

from database import Job, User
from jobs import heartbeat, get_job_model


def _seed_job(db, *, job_id: str = "hbtest1234567", last_progress_at=None) -> str:
    u = User(
        username=f"u-{job_id}",
        email=f"{job_id}@test.com",
        hashed_password="x", tenant_id="t", role="operator",
    )
    db.add(u)
    db.commit()
    j = Job(
        job_id=job_id, user_id=u.id, tenant_id="t",
        artist="x", filename="x.mp3", style="auto",
        status="processing",
        progress=22,  # mid-render
        last_progress_at=last_progress_at,
    )
    db.add(j)
    db.commit()
    return job_id


def test_heartbeat_bumps_last_progress_at(db):
    """Caso happy: heartbeat() actualiza last_progress_at sin tocar nada más."""
    old_ts = datetime.now(timezone.utc) - timedelta(minutes=30)
    jid = _seed_job(db, last_progress_at=old_ts)

    pre = get_job_model(db, jid)
    db.refresh(pre)
    pre_status = pre.status
    pre_progress = pre.progress
    pre_last = pre.last_progress_at

    ok = heartbeat(jid)
    assert ok is True

    post = get_job_model(db, jid)
    db.refresh(post)
    # last_progress_at debe haber avanzado.
    assert post.last_progress_at > pre_last, (
        f"heartbeat did not bump last_progress_at: {pre_last} → {post.last_progress_at}"
    )
    # Otros campos intactos.
    assert post.status == pre_status, "heartbeat must not touch status"
    assert post.progress == pre_progress, "heartbeat must not touch progress"


def test_heartbeat_missing_job_returns_false(db):
    """Job inexistente → False, no raise."""
    ok = heartbeat("nonexistxxxx")
    assert ok is False


def test_heartbeat_is_idempotent(db):
    """Multiple heartbeats consecutivos no rompen ni cambian otros campos."""
    jid = _seed_job(db, job_id="hbidempotent")
    assert heartbeat(jid) is True
    assert heartbeat(jid) is True
    assert heartbeat(jid) is True
    # No side effects on other columns.
    post = get_job_model(db, jid)
    db.refresh(post)
    assert post.status == "processing"
    assert post.progress == 22


def test_heartbeat_swallows_errors_returns_false(db, monkeypatch):
    """Si la DB tira excepción, heartbeat retorna False (no raise)."""
    import database as db_module

    class BrokenSession:
        def query(self, *a, **k): raise RuntimeError("simulated DB drop")
        def rollback(self): pass
        def close(self): pass

    # SessionLocal vive en database; heartbeat hace `from database import
    # SessionLocal` local — monkeypatch debe llegar ahí.
    monkeypatch.setattr(db_module, "SessionLocal", lambda: BrokenSession())
    ok = heartbeat("anyjob123456")
    assert ok is False
