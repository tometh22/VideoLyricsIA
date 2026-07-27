"""Sentry capture in RQ failure callbacks (UMG-launch hardening 2026-06-01).

Before this hardening, the worker process never initialized Sentry, so a
permanently-dead render/transcription/edit was invisible to operators —
the only signal was the user reporting "se quedó en error". The failure
callbacks are the chokepoint where every exhausted job lands, so they now
emit a tagged Sentry event (job_id + tenant_id + layer) before flipping
the DB row.

These tests pin three contracts:
  1. Each callback fires exactly one Sentry capture with the right tags.
  2. The capture NEVER breaks the callback's real work (DB row → error).
  3. A broken/missing sentry_sdk is swallowed silently (best-effort).

Fake-module technique: _capture_job_failure does `import sentry_sdk`
inside the function, so injecting a stand-in into sys.modules is enough —
no real DSN, no network.
"""

import sys
import types
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from database import Job, SessionLocal
from queue_jobs import (
    _capture_job_failure,
    edit_failure_callback,
    pipeline_failure_callback,
    transcription_failure_callback,
)

TENANT = "tenant_sentry_cb_test"


# ---------------------------------------------------------------------------
# Fake sentry_sdk
# ---------------------------------------------------------------------------

class _FakeScope:
    def __init__(self):
        self.tags = {}
        self.extras = {}

    def set_tag(self, k, v):
        self.tags[k] = v

    def set_extra(self, k, v):
        self.extras[k] = v


class _FakeSentry:
    """Records every capture + the scope tags active at capture time."""

    def __init__(self):
        self.exceptions = []   # list of (exc, tags)
        self.messages = []     # list of (msg, level, tags)
        self._scope = None

    def _module(self):
        mod = types.ModuleType("sentry_sdk")
        fake = self

        @contextmanager
        def push_scope():
            fake._scope = _FakeScope()
            try:
                yield fake._scope
            finally:
                pass  # keep the scope so asserts can read the tags

        def capture_exception(exc=None):
            fake.exceptions.append((exc, dict(fake._scope.tags if fake._scope else {})))

        def capture_message(msg, level="info"):
            fake.messages.append((msg, level, dict(fake._scope.tags if fake._scope else {})))

        mod.push_scope = push_scope
        mod.capture_exception = capture_exception
        mod.capture_message = capture_message
        return mod


def _install_fake_sentry(monkeypatch):
    fake = _FakeSentry()
    monkeypatch.setitem(sys.modules, "sentry_sdk", fake._module())
    return fake


# ---------------------------------------------------------------------------
# DB helpers (mirrors test_queue_retry.py)
# ---------------------------------------------------------------------------

def _seed_job(db, *, status: str = "processing", job_id: str | None = None) -> str:
    jid = job_id or f"scb_{uuid.uuid4().hex[:8]}"
    db.add(Job(
        job_id=jid,
        user_id=1,
        tenant_id=TENANT,
        artist="Test Artist",
        filename="x.mp3",
        style="oscuro",
        status=status,
        progress=22,
        current_step="background",
        delivery_profile="youtube",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    ))
    db.commit()
    return jid


def _cleanup(db):
    db.query(Job).filter(Job.tenant_id == TENANT).delete()
    db.commit()


def _fake_rq_job(rq_id: str):
    return SimpleNamespace(id=rq_id)


# ---------------------------------------------------------------------------
# _capture_job_failure unit
# ---------------------------------------------------------------------------

def test_capture_tags_layer_job_and_tenant(monkeypatch):
    fake = _install_fake_sentry(monkeypatch)
    db = SessionLocal()
    try:
        _cleanup(db)
        jid = _seed_job(db)
    finally:
        db.close()

    exc = ValueError("ffmpeg exploded")
    _capture_job_failure("render_pipeline", jid, ValueError, exc)

    assert len(fake.exceptions) == 1, "exactly one capture_exception expected"
    captured_exc, tags = fake.exceptions[0]
    assert captured_exc is exc
    assert tags["layer"] == "render_pipeline"
    assert tags["job_id"] == jid
    assert tags["tenant_id"] == TENANT, (
        "tenant_id must be tagged — incident severity depends on whose job died"
    )

    db = SessionLocal()
    try:
        _cleanup(db)
    finally:
        db.close()


def test_capture_falls_back_to_message_without_exception_value(monkeypatch):
    """SIGKILL / AbandonedJobError paths sometimes hand the callback a
    class but no instance — we still want a Sentry event."""
    fake = _install_fake_sentry(monkeypatch)

    class _Abandoned(Exception):
        pass

    _capture_job_failure("transcription", "ghost_job_123", _Abandoned, None)

    assert len(fake.exceptions) == 0
    assert len(fake.messages) == 1
    msg, level, tags = fake.messages[0]
    assert level == "error"
    assert "ghost_job_123" in msg
    assert "_Abandoned" in msg
    assert tags["layer"] == "transcription"


def test_capture_swallows_broken_sentry(monkeypatch):
    """Best-effort contract: a sentry_sdk that raises must never
    propagate into the callback (it would derail RQ's failure
    bookkeeping)."""
    broken = types.ModuleType("sentry_sdk")

    def _boom(*a, **kw):
        raise RuntimeError("sentry is down")

    broken.push_scope = _boom
    broken.capture_exception = _boom
    broken.capture_message = _boom
    monkeypatch.setitem(sys.modules, "sentry_sdk", broken)

    # Must not raise.
    _capture_job_failure("edit", "whatever", RuntimeError, RuntimeError("x"))


# ---------------------------------------------------------------------------
# Callback integration: capture fires AND the DB row still flips to error
# ---------------------------------------------------------------------------

def test_pipeline_callback_captures_and_marks_error(monkeypatch):
    fake = _install_fake_sentry(monkeypatch)
    db = SessionLocal()
    try:
        _cleanup(db)
        jid = _seed_job(db)

        boom = RuntimeError("ffmpeg returned non-zero status: -9")
        pipeline_failure_callback(_fake_rq_job(jid), None, RuntimeError, boom, None)

        # 1. Sentry got the event with layer + job tags.
        assert len(fake.exceptions) == 1
        _, tags = fake.exceptions[0]
        assert tags["layer"] == "render_pipeline"
        assert tags["job_id"] == jid

        # 2. The callback's real work still happened.
        row = db.query(Job).filter(Job.job_id == jid).first()
        db.refresh(row)
        assert row.status == "error"
        assert "ffmpeg" in (row.error or "").lower()
    finally:
        _cleanup(db)
        db.close()


def test_transcription_callback_captures_with_prefix_stripped(monkeypatch):
    """RQ transcription job ids carry a `transcribe:` prefix — the Sentry
    tag must use OUR job_id (what operators search by), not the RQ id."""
    fake = _install_fake_sentry(monkeypatch)
    db = SessionLocal()
    try:
        _cleanup(db)
        jid = _seed_job(db, status="transcribing")

        transcription_failure_callback(
            _fake_rq_job(f"transcribe:{jid}"), None,
            RuntimeError, RuntimeError("whisper OOM"), None,
        )

        assert len(fake.exceptions) == 1
        _, tags = fake.exceptions[0]
        assert tags["layer"] == "transcription"
        assert tags["job_id"] == jid, "tag must be the DB job_id, not the RQ id"

        row = db.query(Job).filter(Job.job_id == jid).first()
        db.refresh(row)
        assert row.status == "transcription_failed"
    finally:
        _cleanup(db)
        db.close()


def test_edit_callback_captures_with_prefix_stripped(monkeypatch):
    fake = _install_fake_sentry(monkeypatch)
    db = SessionLocal()
    try:
        _cleanup(db)
        jid = _seed_job(db, status="editing")

        edit_failure_callback(
            _fake_rq_job(f"edit:{jid}"), None,
            RuntimeError, RuntimeError("edit pipeline died"), None,
        )

        assert len(fake.exceptions) == 1
        _, tags = fake.exceptions[0]
        assert tags["layer"] == "edit"
        assert tags["job_id"] == jid

        row = db.query(Job).filter(Job.job_id == jid).first()
        db.refresh(row)
        assert row.status == "error"
    finally:
        _cleanup(db)
        db.close()


def test_callback_db_work_survives_sentry_failure(monkeypatch):
    """The whole point of best-effort: even if Sentry capture explodes,
    the user-facing outcome (row → error with Spanish message) must be
    identical to the no-Sentry behavior."""
    broken = types.ModuleType("sentry_sdk")

    def _boom(*a, **kw):
        raise RuntimeError("sentry sdk is broken")

    broken.push_scope = _boom
    broken.capture_exception = _boom
    broken.capture_message = _boom
    monkeypatch.setitem(sys.modules, "sentry_sdk", broken)

    db = SessionLocal()
    try:
        _cleanup(db)
        jid = _seed_job(db)

        class _AbandonedJobError(Exception):
            pass

        pipeline_failure_callback(
            _fake_rq_job(jid), None, _AbandonedJobError, _AbandonedJobError(), None,
        )

        row = db.query(Job).filter(Job.job_id == jid).first()
        db.refresh(row)
        assert row.status == "error"
        assert "reintentar" in (row.error or "").lower()
    finally:
        _cleanup(db)
        db.close()
