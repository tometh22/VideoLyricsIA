"""Recordatorio de pending_review estancado (reaper.remind_stale_pending_review)
y default ON de la notificación de job terminado.

Contexto (datos de producción 2026-07-03): con REQUIRE_REVIEW los renders
terminan en pending_review, el email "video listo" estaba detrás de
notif_jobs default False y ningún usuario lo tenía activado — el primer
video de una operadora de UMG Chile quedó 8 días esperando sin que nadie
lo supiera. El sweep manda UN email por job (dedupe por AuditLog) y
respeta el opt-out explícito.
"""
import uuid as _uuid
from datetime import datetime, timedelta, timezone

from database import Job, User, UserSettings, AuditLog
from reaper import remind_stale_pending_review


def _mkuser(db, *, email="op@umusic.com", notif_jobs=None):
    u = User(
        username=f"op_{_uuid.uuid4().hex[:8]}",
        email=f"{_uuid.uuid4().hex[:6]}+{email}",
        hashed_password="x",
        tenant_id=f"t_{_uuid.uuid4().hex[:8]}",
    )
    db.add(u)
    db.commit()
    if notif_jobs is not None:
        db.add(UserSettings(user_id=u.id, settings_json={"notif_jobs": notif_jobs}))
        db.commit()
    return u


def _mkjob(db, user, *, status="pending_review", age_hours=72):
    j = Job(
        job_id=_uuid.uuid4().hex[:12],
        user_id=user.id,
        tenant_id=user.tenant_id,
        artist="La Mosca",
        song_title="Para No Verte Mas",
        filename="a.mp3",
        status=status,
        created_at=datetime.now(timezone.utc) - timedelta(hours=age_hours),
    )
    db.add(j)
    db.commit()
    return j


def _capture(monkeypatch):
    calls = []
    import emails
    monkeypatch.setattr(
        emails, "send_review_reminder",
        lambda **kw: calls.append(kw),
    )
    return calls


def test_reminder_sent_once_per_job(db, monkeypatch):
    calls = _capture(monkeypatch)
    u = _mkuser(db)
    j = _mkjob(db, u, age_hours=72)

    assert remind_stale_pending_review(db) == 1
    assert len(calls) == 1
    assert calls[0]["job_id"] == j.job_id
    assert calls[0]["days_waiting"] == 3
    # AuditLog registrado para el dedupe.
    rows = db.query(AuditLog).filter(AuditLog.action == "job.review_reminder").all()
    assert any((r.detail or {}).get("job_id") == j.job_id for r in rows)

    # Segundo pass: no re-manda.
    assert remind_stale_pending_review(db) == 0
    assert len(calls) == 1


def test_fresh_jobs_not_reminded(db, monkeypatch):
    calls = _capture(monkeypatch)
    u = _mkuser(db)
    _mkjob(db, u, age_hours=12)  # < 48 h
    assert remind_stale_pending_review(db) == 0
    assert calls == []


def test_non_pending_review_ignored(db, monkeypatch):
    calls = _capture(monkeypatch)
    u = _mkuser(db)
    _mkjob(db, u, status="done", age_hours=200)
    assert remind_stale_pending_review(db) == 0
    assert calls == []


def test_optout_skips_email_but_records_audit(db, monkeypatch):
    """notif_jobs=False explícito: sin email, pero con AuditLog para que
    el sweep no re-escanee ese job en cada ciclo."""
    calls = _capture(monkeypatch)
    u = _mkuser(db, notif_jobs=False)
    j = _mkjob(db, u, age_hours=72)
    assert remind_stale_pending_review(db) == 0
    assert calls == []
    rows = db.query(AuditLog).filter(AuditLog.action == "job.review_reminder").all()
    mine = [r for r in rows if (r.detail or {}).get("job_id") == j.job_id]
    assert mine and mine[0].detail.get("emailed") is False
    # Y no se re-procesa.
    assert remind_stale_pending_review(db) == 0


def test_disabled_via_threshold_zero(db, monkeypatch):
    calls = _capture(monkeypatch)
    u = _mkuser(db)
    _mkjob(db, u, age_hours=72)
    assert remind_stale_pending_review(db, threshold_h=0) == 0
    assert calls == []


def test_job_completed_email_defaults_on_and_flags_review():
    """El gate de pipeline usa _prefs.get('notif_jobs', True) y pasa
    needs_review; acá pinneamos que send_job_completed acepta el kwarg
    (regresión de firma) sin mandar nada (SMTP no configurado en tests)."""
    import emails
    emails.send_job_completed(
        email="x@y.z", username="u", artist="A", filename="s.mp3",
        job_id="abc123", needs_review=True,
    )
