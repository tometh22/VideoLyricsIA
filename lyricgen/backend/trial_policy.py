"""Opt-in, bounded trials. Disabled unless a billing group is explicitly listed.

Existing CreditGrant/AuditLog tables form an append-only admission ledger; no
schema migration or change to ordinary paid-plan accounting. The caller must
hold the account quota advisory lock until reservation + outbox commit.
"""
import os
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import text
from database import AuditLog, CreditGrant

ACTIVATION_REASON = "bounded_trial_v1"
RESERVATION_ACTION = "trial.credit_reserved"
TRANSCRIPTION_ACTION = "trial.transcription_attempt"
TRANSCRIPTION_ATTEMPT_CAP = 6


def configured_group(group):
    return bool(group) and group in {
        value.strip() for value in os.getenv("TRIAL_BILLING_GROUPS", "").split(",")
        if value.strip()
    }


def applies(identity):
    return configured_group(identity.get("billing_group"))


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def latest_grant(db, group):
    return db.query(CreditGrant).filter(
        CreditGrant.billing_group == group,
        CreditGrant.reason == ACTIVATION_REASON,
    ).order_by(CreditGrant.id.desc()).first()


def reservations(db, grant):
    if grant is None:
        return {}
    rows = db.query(AuditLog.detail).filter(
        AuditLog.action == RESERVATION_ACTION,
        AuditLog.created_at >= grant.granted_at,
    ).all()
    # Defensively deduplicate by job. Reservations never refund on reject or
    # delete: those actions cannot recover money already spent at a provider.
    result = {}
    for (detail,) in rows:
        if isinstance(detail, dict) and detail.get("grant_id") == grant.id:
            key = detail.get("job_id")
            result[key] = max(result.get(key, 0), int(detail.get("credits", 0)))
    return result


def snapshot(db, group, now=None):
    now = now or datetime.now(timezone.utc)
    grant = latest_grant(db, group)
    used = sum(reservations(db, grant).values())
    total = grant.amount if grant else 9
    expires = utc(grant.expires_at) if grant and grant.expires_at else None
    state = "pending"
    if grant:
        if grant.revoked or not expires or now >= expires:
            state = "expired"
        elif now < utc(grant.granted_at):
            state = "pending"
        else:
            state = "exhausted" if used >= total else "active"
    return {
        "state": state, "credits": total, "reserved": used,
        "available": max(0, total - used) if state == "active" else 0,
        "starts_at": utc(grant.granted_at).isoformat() if grant else None,
        "expires_at": expires.isoformat() if expires else None,
        "grant_id": grant.id if grant else None,
    }


def require_open(db, identity):
    if not applies(identity):
        return None
    info = snapshot(db, identity["billing_group"])
    if info["state"] in ("pending", "expired"):
        code = "trial_not_started" if info["state"] == "pending" else "trial_expired"
        message = ("Tu trial todavía no está activado. Coordiná el inicio con el equipo de Genly."
                   if code == "trial_not_started" else
                   "Finalizaron las 24 horas del trial. Contactá al equipo de Genly para continuar.")
        raise HTTPException(403, detail={"code": code, "message": message, "trial": info})
    return info


def require_job_open(job_id):
    """Worker admission, before storage/provider work. Fail closed on DB errors."""
    if not os.getenv("TRIAL_BILLING_GROUPS", "").strip():
        return
    from database import Job, User, SessionLocal
    with SessionLocal() as db:
        job = db.query(Job).filter(Job.job_id == job_id).first()
        owner = db.query(User).filter(User.id == job.user_id).first() if job else None
        if owner is None:
            raise HTTPException(409, detail={"code": "trial_owner_missing", "message": "No se pudo verificar la cuenta del trabajo."})
        require_open(db, owner.to_dict())


def transcription_attempts(db, grant):
    """All admitted attempts in this grant, including repeated job IDs."""
    if grant is None:
        return []
    rows = db.query(AuditLog.detail).filter(
        AuditLog.action == TRANSCRIPTION_ACTION,
        AuditLog.created_at >= grant.granted_at,
    ).all()
    return [detail for (detail,) in rows
            if isinstance(detail, dict)
            and detail.get("grant_id") == grant.id
            and detail.get("billing_group") == grant.billing_group]


def _require_recorded_job_admission(job_id, *, transcription):
    """Read only: workers verify the source owner's current grant, never admit."""
    if not os.getenv("TRIAL_BILLING_GROUPS", "").strip():
        return
    from database import Job, User, SessionLocal
    with SessionLocal() as db:
        job = db.query(Job).filter(Job.job_id == job_id).first()
        owner = db.query(User).filter(User.id == job.user_id).first() if job else None
        if owner is None:
            raise HTTPException(409, detail={
                "code": "trial_owner_missing",
                "message": "No se pudo verificar la cuenta del trabajo.",
            })
        identity = owner.to_dict()
        info = require_open(db, identity)
        if info is None:
            return
        grant = latest_grant(db, identity["billing_group"])
        if transcription:
            admitted = any(row.get("job_id") == job_id
                           for row in transcription_attempts(db, grant))
            code = "trial_transcription_unadmitted"
            message = "Esta transcripción no tiene un intento autorizado en el trial actual."
        else:
            from auth import scenes_credit_cost
            params = job.render_params or {}
            plan = job.scene_plan or {}
            required = scenes_credit_cost() if params.get("enable_scenes") or "scenes" in plan else 1
            admitted = reservations(db, grant).get(job_id, 0) >= required
            code = "trial_job_not_reserved"
            message = "Este video no tiene créditos reservados en el trial actual."
        if not admitted:
            raise HTTPException(403 if transcription else 409,
                                detail={"code": code, "message": message, "trial": info})
        return info


def require_job_reserved(job_id):
    """Render/edit gate: clock plus a sufficient current-grant reservation.

    Exhaustion does not cancel previously reserved work. Expiry does. No
    reservation is inferred from an old grant, another job, or transcription.
    Persisted Scenes selection/plan requires scenes_credit_cost (default 3).
    """
    return _require_recorded_job_admission(job_id, transcription=False)


def require_transcription_admitted(job_id):
    """Transcription worker gate: active clock plus a recorded current-grant attempt.

    Queued admitted work may finish after credits are exhausted, within the
    trial clock. Queue deduplication/retry limits remain the caller's concern.
    """
    return _require_recorded_job_admission(job_id, transcription=True)


def admit_transcription(db, identity, job_id):
    """Reserve one of six shared transcription attempts; caller commits/enqueues.

    Every invocation counts, including a retry of the same job. Hold the same
    transaction through publication: rollback releases this attempt. PostgreSQL
    serializes count-and-append with the render reservation/activation lock.
    """
    if not applies(identity):
        return
    lock_budget(db, identity["billing_group"])
    info = require_open(db, identity)
    grant = latest_grant(db, identity["billing_group"])
    if info["state"] == "exhausted" and reservations(db, grant).get(job_id, 0) <= 0:
        raise HTTPException(402, detail={
            "code": "trial_credits_exhausted",
            "message": "El trial agotó sus créditos. No se pueden iniciar transcripciones de videos sin reserva.",
            "trial": info,
        })
    if len(transcription_attempts(db, grant)) >= TRANSCRIPTION_ATTEMPT_CAP:
        raise HTTPException(429, detail={
            "code": "trial_transcription_limit",
            "message": "El trial alcanzó el límite de seis intentos de transcripción.",
        })
    db.add(AuditLog(user_id=identity["id"], action=TRANSCRIPTION_ACTION, detail={
        "grant_id": grant.id, "billing_group": identity["billing_group"],
        "job_id": job_id,
    }))
    db.flush()
    return info


def require_budget(db, identity, credits, job_id=None):
    info = require_open(db, identity)
    if info is None:
        return
    grant = latest_grant(db, identity["billing_group"])
    already = reservations(db, grant).get(job_id, 0) if job_id else 0
    if info["available"] < max(0, credits - already):
        raise HTTPException(402, detail={
            "code": "trial_credits_exhausted",
            "message": f"Este video necesita {credits} créditos y quedan {info['available']} disponibles en el trial. Los videos ya reservados pueden terminarse dentro de las 24 horas.",
            "trial": info,
        })


def lock_budget(db, group):
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"),
                   {"scope": "trial-budget:" + group})
    elif dialect != "sqlite":
        raise RuntimeError("Trial accounting requires PostgreSQL")


def reserve(db, identity, job_id, credits, purpose="generate"):
    if not applies(identity):
        return
    lock_budget(db, identity["billing_group"])
    require_budget(db, identity, credits, job_id)
    grant = latest_grant(db, identity["billing_group"])
    attempts = sum(1 for (d,) in db.query(AuditLog.detail).filter(
        AuditLog.action == "trial.render_attempt", AuditLog.created_at >= grant.granted_at,
    ).all() if isinstance(d, dict) and d.get("grant_id") == grant.id and d.get("job_id") == job_id)
    if attempts >= 3:
        raise HTTPException(429, detail={"code": "trial_retry_limit", "message": "Este video alcanzó el límite de reintentos del trial. Contactá al equipo de Genly."})
    db.add(AuditLog(user_id=identity["id"], action="trial.render_attempt", detail={
        "grant_id": grant.id, "job_id": job_id, "purpose": purpose,
    }))
    prior = reservations(db, grant).get(job_id, 0)
    if prior >= credits:
        db.flush()
        return
    db.add(AuditLog(user_id=identity["id"], action=RESERVATION_ACTION, detail={
        "billing_group": identity["billing_group"], "grant_id": grant.id,
        "job_id": job_id, "credits": credits,
    }))
    db.flush()


def usage(db, group, scene_cost=3):
    info = snapshot(db, group)
    used, limit, available = info["reserved"], info["credits"], info["available"]
    return {
        "plan": "trial", "limit": limit, "used": used, "remaining": available,
        "overage": 0, "overage_cost_per_video": 0, "overage_total": 0,
        "monthly_price": 0, "percent": min(100, round(used / limit * 100)) if limit else 0,
        "alert_80": used >= limit * .8, "alert_100": used >= limit,
        "scenes_credit_cost": scene_cost, "bonus_total": 0, "bonus_used": 0,
        "bonus_remaining": 0, "bonus_expires_at": info["expires_at"],
        "total_available": available,
        "projection": {"normal": available, "escenas": available // scene_cost},
        "trial": info,
    }


def activate(db, group, actor_id):
    """Explicit admin operation under account lock. Never auto-start on login.

    Retries are idempotent and cannot reset the clock or replenish credits.
    A second trial requires a separately authorized operational decision.
    """
    if not configured_group(group):
        raise HTTPException(404, detail="Trial group is not configured")
    lock_budget(db, group)
    existing = latest_grant(db, group)
    if existing:
        return snapshot(db, group)
    now = datetime.now(timezone.utc)
    db.add(CreditGrant(billing_group=group, amount=9, reason=ACTIVATION_REASON,
                       granted_by=actor_id, granted_at=now,
                       expires_at=now + timedelta(hours=24), revoked=False))
    db.flush()
    return snapshot(db, group)
