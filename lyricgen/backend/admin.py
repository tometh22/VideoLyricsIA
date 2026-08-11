"""Admin panel API for GenLy AI."""

import asyncio
import logging
import os
import shutil
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, File, UploadFile, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from auth import (
    get_current_user,
    PLANS,
    pwd_context,
    telemetry_enabled,
    validate_password_strength,
    is_explicitly_local_environment,
    is_super_admin,
)
from database import User, Job, Invoice, AuditLog, AIProvenance, AssetUsage, BackgroundAsset, UserSession, LoginSession, get_db
from error_taxonomy import classify_error

BACKGROUNDS_DIR = os.path.join(os.path.dirname(__file__), "..", "assets", "backgrounds", "library")
os.makedirs(BACKGROUNDS_DIR, exist_ok=True)

logger = logging.getLogger("genly.admin")

router = APIRouter(prefix="/admin", tags=["admin"])


def require_admin(current_user: dict = Depends(get_current_user)):
    """Dependency that ensures the user is an admin."""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user


# _super_admin_allowlist vive en auth.py (se movió para computar el flag
# is_super_admin en get_current_user sin import circular); se re-importa
# arriba para mantener compatibilidad con quien lo use desde admin.


def require_super_admin(current_user: dict = Depends(get_current_user)):
    """Admin + allowlist opcional para vistas de observabilidad de usuarios.

    La actividad por usuario (qué hace cada operador, sus errores, su
    tiempo de uso) es información operativa de la plataforma, no del
    cliente: un admin local de un tenant no debería verla. Cuando
    SUPER_ADMIN_USERS está seteado (ej. en prod:
    "tomas@epical.digital,agus.cafisi"), solo esos usuarios —
    identificados por username o email — pasan. Sin la var, dev/tests
    conservan el fallback de admin; cualquier entorno desplegado o
    desconocido falla cerrado.
    """
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    if not is_super_admin(
        current_user.get("username"),
        current_user.get("email"),
        current_user.get("role"),
    ):
        raise HTTPException(status_code=403, detail="Super admin access required")
    return current_user


def _requires_durable_background_storage() -> bool:
    """Only explicitly local environments may persist catalogue files locally."""
    return not is_explicitly_local_environment()


class SubmissionsControlRequest(BaseModel):
    paused: bool
    reason: str = Field(default="", max_length=500)
    until: Optional[datetime] = None
    retry_after: int = Field(default=60, ge=1, le=3600)


class GlobalLogoutRequest(BaseModel):
    confirmation: str
    reason: str = Field(default="security cutover", max_length=500)


@router.get("/ops/submissions")
def get_submissions_control(
    admin: dict = Depends(require_super_admin),
):
    from ops_control import get_submissions_state
    return get_submissions_state()


@router.put("/ops/submissions")
def update_submissions_control(
    body: SubmissionsControlRequest,
    admin: dict = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    from ops_control import set_submissions_state
    try:
        state = set_submissions_state(
            paused=body.paused,
            reason=body.reason,
            until=body.until,
            retry_after=body.retry_after,
        )
    except Exception as exc:
        logger.exception("Could not update submissions control")
        raise HTTPException(status_code=503, detail="Operations control unavailable") from exc
    db.add(AuditLog(
        user_id=admin.get("id"),
        action="ops.submissions.updated",
        detail={
            "paused": state["paused"],
            "reason": state["reason"],
            "until": state.get("until"),
            "retry_after": state["retry_after"],
        },
    ))
    db.commit()
    return state


@router.post("/ops/logout-all")
def logout_all_users(
    body: GlobalLogoutRequest,
    admin: dict = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    """Irreversibly invalidate every browser access token and session."""
    if body.confirmation != "LOGOUT_ALL_USERS":
        raise HTTPException(status_code=400, detail="Explicit confirmation required")
    now = datetime.now(timezone.utc)
    users_updated = db.query(User).update(
        {User.auth_version: User.auth_version + 1},
        synchronize_session=False,
    )
    sessions_revoked = (
        db.query(LoginSession)
        .filter(LoginSession.revoked_at.is_(None))
        .update({LoginSession.revoked_at: now}, synchronize_session=False)
    )
    db.add(AuditLog(
        user_id=admin.get("id"),
        action="auth.global_logout",
        detail={
            "reason": body.reason,
            "users_updated": users_updated,
            "sessions_revoked": sessions_revoked,
        },
    ))
    db.commit()
    return {
        "ok": True,
        "users_updated": users_updated,
        "sessions_revoked": sessions_revoked,
    }


# ---------------------------------------------------------------------------
# Dashboard stats
# ---------------------------------------------------------------------------

@router.get("/stats")
async def admin_stats(
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Global platform statistics."""
    total_users = db.query(User).count()
    active_users = db.query(User).filter(User.is_active == True).count()
    total_jobs = db.query(Job).count()
    done_jobs = db.query(Job).filter(Job.status == "done").count()
    error_jobs = db.query(Job).filter(Job.status == "error").count()
    processing_jobs = db.query(Job).filter(Job.status == "processing").count()
    pending_review_jobs = db.query(Job).filter(Job.status == "pending_review").count()

    # Revenue
    total_revenue_cents = db.query(func.sum(Invoice.amount_cents)).filter(
        Invoice.status == "paid"
    ).scalar() or 0

    # Monthly stats
    now = datetime.now(timezone.utc)
    month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    monthly_jobs = db.query(Job).filter(Job.created_at >= month_start).count()
    monthly_revenue_cents = db.query(func.sum(Invoice.amount_cents)).filter(
        Invoice.status == "paid",
        Invoice.created_at >= month_start,
    ).scalar() or 0

    # Plan distribution
    plan_dist = (
        db.query(User.plan_id, func.count(User.id))
        .filter(User.is_active == True)
        .group_by(User.plan_id)
        .all()
    )

    return {
        "users": {
            "total": total_users,
            "active": active_users,
        },
        "jobs": {
            "total": total_jobs,
            "done": done_jobs,
            "errors": error_jobs,
            "processing": processing_jobs,
            "pending_review": pending_review_jobs,
            "this_month": monthly_jobs,
        },
        "revenue": {
            "total": total_revenue_cents / 100,
            "this_month": monthly_revenue_cents / 100,
            "currency": "usd",
        },
        "plans": {p: c for p, c in plan_dist},
    }


# ---------------------------------------------------------------------------
# User management
# ---------------------------------------------------------------------------

@router.get("/users")
async def list_users(
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    search: str = Query(""),
):
    """List all users with optional search."""
    query = db.query(User).order_by(User.created_at.desc())
    if search:
        query = query.filter(
            (User.username.ilike(f"%{search}%")) |
            (User.email.ilike(f"%{search}%")) |
            (User.tenant_id.ilike(f"%{search}%"))
        )
    total = query.count()
    users = query.offset(offset).limit(limit).all()

    result = []
    for u in users:
        user_dict = u.to_dict()
        # Add job count
        job_count = db.query(Job).filter(Job.user_id == u.id).count()
        user_dict["job_count"] = job_count
        result.append(user_dict)

    return {"total": total, "users": result}


@router.get("/users/{user_id}")
async def get_user_detail(
    user_id: int,
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Get detailed user info with usage stats."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Usage
    now = datetime.now(timezone.utc)
    month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    monthly_jobs = db.query(Job).filter(
        Job.user_id == user.id,
        Job.status == "done",
        Job.created_at >= month_start,
    ).count()

    total_jobs = db.query(Job).filter(Job.user_id == user.id).count()
    total_invoices = db.query(Invoice).filter(Invoice.user_id == user.id).count()

    user_dict = user.to_dict()
    user_dict["stats"] = {
        "total_jobs": total_jobs,
        "monthly_jobs": monthly_jobs,
        "total_invoices": total_invoices,
    }

    return user_dict


class CreateUserRequest(BaseModel):
    username: str
    password: str
    email: str = ""
    role: str = "user"
    plan_id: str = "100"
    # tenant_id: when omitted, auth.create_user() auto-generates it from
    # the username — fine for solo accounts but produces an isolated
    # tenant per user. Pass it explicitly to put several teammates into
    # the same shared workspace (e.g. all UMG operators on
    # tenant_id="universal_music" so they see each other's jobs).
    tenant_id: str = ""
    # If true, the user keeps generating past plan monthly limit and we
    # invoice the overage out-of-band. Default False = hard wall at limit.
    allow_overage: bool = False
    # UMG Guideline 5: gate for AI tool usage. Default False keeps the
    # current "auth-after-create" workflow intact. When True, the same
    # transaction sets ai_authorized_at/by so the operator doesn't have
    # to follow up with a separate authorize-ai call.
    ai_authorized: bool = False
    # Cuenta de facturación compartida entre tenants. Ej: usuarios de
    # universal_argentina y universal_chile con billing_group
    # "universal_music" consumen del mismo plan mensual aunque no se vean
    # los videos entre sí. Vacío = sin grupo (cuota por tenant).
    billing_group: str = ""


@router.post("/users")
async def create_user_admin(
    body: CreateUserRequest,
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Create a new user (admin).

    Note: the admin form defaults `ai_authorized=False` (opposite of the
    public `/auth/register` default) because admin-created accounts
    typically belong to regulated tenants like UMG that require an
    explicit auth step. To create with AI enabled, pass
    `ai_authorized=True` — the same transaction stamps the audit fields.
    """
    from auth import create_user
    try:
        # enforce_reserved=False because this endpoint IS the
        # "admin seeding script" path that auth.create_user's docstring
        # documents as needing the bypass. The reserved-tenant guard
        # (and the sibling "tenant already exists" collision check) are
        # there to stop self-registered users from squatting on
        # reserved tenant names ("umg", "warner") or attaching
        # themselves to an existing tenant via /auth/register. When the
        # ADMIN is the one assigning the tenant — like here, when
        # placing UMG operators into tenant_id="umusic" — both checks
        # are unwanted: it's a deliberate team-workspace assignment.
        # Without this, admin-managed teams can only ever have one user
        # (whoever was created first claims the tenant; later users in
        # the same team get rejected with "Tenant 'X' already exists").
        user = create_user(
            db,
            username=body.username,
            password=body.password,
            email=body.email or None,
            role=body.role,
            plan=body.plan_id,
            tenant_id=body.tenant_id.strip() or None,
            ai_authorized=body.ai_authorized,
            enforce_reserved=False,
        )
        _post_create_dirty = False
        if body.allow_overage:
            user.allow_overage = True
            _post_create_dirty = True
        if body.ai_authorized:
            # Stamp audit fields so the authorization is attributable to
            # the admin that created the user, mirroring authorize-ai.
            user.ai_authorized_at = datetime.now(timezone.utc)
            user.ai_authorized_by = admin["id"]
            _post_create_dirty = True
        if body.billing_group.strip():
            user.billing_group = body.billing_group.strip().lower()
            _post_create_dirty = True
        if _post_create_dirty:
            db.commit()
            db.refresh(user)
        return user.to_dict()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/users/{user_id}/authorize-ai")
async def authorize_ai(
    user_id: int,
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Authorize a user to use AI tools (UMG Guideline 5)."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.ai_authorized = True
    user.ai_authorized_at = datetime.now(timezone.utc)
    user.ai_authorized_by = admin["id"]
    db.add(AuditLog(
        user_id=admin["id"],
        action="admin.authorize_ai",
        detail={"target_user": user_id},
    ))
    db.commit()
    return {"ok": True, "user_id": user_id, "ai_authorized": True}


@router.post("/users/{user_id}/revoke-ai")
async def revoke_ai(
    user_id: int,
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Revoke AI tool authorization from a user."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.ai_authorized = False
    user.ai_authorized_at = None
    user.ai_authorized_by = None
    db.add(AuditLog(
        user_id=admin["id"],
        action="admin.revoke_ai",
        detail={"target_user": user_id},
    ))
    db.commit()
    return {"ok": True, "user_id": user_id, "ai_authorized": False}


class UpdateUserRequest(BaseModel):
    plan_id: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None
    email: Optional[str] = None
    password: Optional[str] = None
    # Per-tenant volume cap. Set to None to use system default; set to an
    # integer to override (e.g. raise to 200 for a high-volume tenant).
    max_videos_per_day: Optional[int] = None
    # Per-tenant concurrent-jobs cap (a.k.a. batch size). Default is 10.
    # Raise for tenants that ship full albums (12-15 tracks) as one batch.
    max_concurrent_jobs: Optional[int] = None
    # B2B / overage opt-in. True = user can keep generating past plan
    # monthly limit (extra videos invoice out-of-band).
    allow_overage: Optional[bool] = None
    # Mover al usuario a otro workspace (tenant). Si move_jobs es True
    # (default), sus videos se mueven con él — así no pierde su historial.
    tenant_id: Optional[str] = None
    move_jobs: bool = True
    # Cuenta de facturación compartida. String vacío = sacar del grupo
    # (volver a cuota por tenant). None = sin cambios.
    billing_group: Optional[str] = None


@router.patch("/users/{user_id}")
async def update_user_admin(
    user_id: int,
    body: UpdateUserRequest,
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Update a user's plan, role, status, workspace (tenant) or billing group."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if body.plan_id is not None and body.plan_id in PLANS:
        user.plan_id = body.plan_id
    if body.role is not None and body.role in ("user", "admin"):
        user.role = body.role
    if body.is_active is not None:
        user.is_active = body.is_active
    if body.email is not None:
        user.email = body.email
    if body.password is not None:
        try:
            validate_password_strength(body.password)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        user.hashed_password = pwd_context.hash(body.password)
    if body.max_videos_per_day is not None:
        # Allow 0 to mean "block all uploads"; clamp to non-negative.
        user.max_videos_per_day = max(0, int(body.max_videos_per_day))
    if body.max_concurrent_jobs is not None:
        # Min 1 — a cap of 0 would block uploads entirely; use is_active=False
        # for that. Clamp negatives to 1.
        user.max_concurrent_jobs = max(1, int(body.max_concurrent_jobs))
    if body.allow_overage is not None:
        user.allow_overage = bool(body.allow_overage)

    # Cambio de workspace (tenant). El admin mueve usuarios entre equipos
    # (ej. reorganización de Universal en AR/CL). Por default los jobs del
    # usuario se mueven con él para que conserve su historial — sin esto,
    # sus videos quedan huérfanos en el tenant viejo (solo visibles para
    # admins) y su Historial aparece vacío.
    moved_jobs = 0
    old_tenant = user.tenant_id
    if body.tenant_id is not None and body.tenant_id.strip():
        new_tenant = body.tenant_id.strip().lower().replace(" ", "_")
        if new_tenant != user.tenant_id:
            user.tenant_id = new_tenant
            if body.move_jobs:
                moved_jobs = (
                    db.query(Job)
                    .filter(Job.user_id == user.id, Job.tenant_id == old_tenant)
                    .update({Job.tenant_id: new_tenant}, synchronize_session=False)
                )

    # Cuenta de facturación: "" = sacar del grupo, no-vacío = asignar.
    if body.billing_group is not None:
        group = body.billing_group.strip().lower()
        user.billing_group = group or None

    db.commit()
    db.refresh(user)

    # Audit
    detail = {"target_user": user_id, "changes": body.model_dump(exclude_none=True)}
    if moved_jobs:
        detail["moved_jobs"] = moved_jobs
        detail["old_tenant"] = old_tenant
    db.add(AuditLog(
        user_id=admin["id"],
        action="admin.update_user",
        detail=detail,
    ))
    db.commit()

    return user.to_dict()


@router.delete("/users/{user_id}")
async def delete_user_admin(
    user_id: int,
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Soft-delete a user: deactivate + anonymise (same contract as the
    self-service /auth/account DELETE).

    Hard delete es imposible sin romper FKs (jobs, invoices, audit apuntan
    a users.id sin CASCADE) y además borraría el rastro de auditoría. El
    soft-delete deja la fila pero:
      - is_active=False → no puede loguearse, no aparece en vistas activas
      - username → deleted_{id}, email → NULL → datos personales fuera

    Guards:
      - Un admin no puede borrarse a sí mismo (lockout accidental).
      - No se puede borrar a otro admin — primero hay que degradarlo a
        user (PATCH role) y después borrarlo. Dos pasos a propósito.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == admin["id"]:
        raise HTTPException(status_code=400, detail="No podés borrar tu propia cuenta de admin.")
    if user.role == "admin":
        raise HTTPException(
            status_code=400,
            detail="No se puede borrar un admin. Primero degradalo a user (PATCH role) y después borralo.",
        )

    old_username = user.username
    user.is_active = False
    user.email = None
    user.username = f"deleted_{user.id}"
    db.add(AuditLog(
        user_id=admin["id"],
        action="admin.delete_user",
        detail={"target_user": user_id, "old_username": old_username, "tenant_id": user.tenant_id},
    ))
    db.commit()
    return {"ok": True, "user_id": user_id, "deleted": True}


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

@router.post("/runbook/reaper-now")
async def runbook_reaper_now(
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
    threshold_min: int = Query(100, ge=10, le=1440),
):
    """Force an immediate reaper pass.

    The reaper already runs every 5 min on its own (see main.py
    on_startup) — this endpoint is for the operator who just spotted
    a zombie in admin and doesn't want to wait for the next cycle.

    Snapshots the to-be-killed jobs before reaping so the response
    can show what was acted on (after the reap they're status=error
    and indistinguishable from normal failures).

    Audited: every invocation lands in AuditLog with the count and
    the operator's user_id. Same guardrails as the auto-pass: only
    jobs in processing/queued past threshold_min get touched.
    """
    from reaper import find_stuck_jobs, reap_all_stuck

    targets = find_stuck_jobs(db, threshold_min)
    snapshot = [
        {
            "job_id": j.job_id,
            "tenant_id": j.tenant_id,
            "artist": j.artist,
            "current_step": j.current_step,
            "progress": j.progress,
        }
        for j in targets
    ]

    # reap_all_stuck owns its own session — we don't pass `db` to it.
    count = reap_all_stuck(threshold_min)

    db.add(AuditLog(
        user_id=admin["id"],
        action="admin.runbook.reaper_now",
        detail={
            "count": count,
            "threshold_min": threshold_min,
            "killed_jobs": [s["job_id"] for s in snapshot],
        },
    ))
    db.commit()

    return {
        "count": count,
        "threshold_min": threshold_min,
        "killed": snapshot,
    }


@router.get("/stuck-jobs")
async def admin_stuck_jobs(
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
    threshold_min: int = Query(100, ge=10, le=1440),
):
    """List jobs that have been in processing/queued longer than
    threshold_min. Used by the admin Overview banner so the operator
    sees zombies before the reaper kills them next pass."""
    from reaper import find_stuck_jobs
    stuck = find_stuck_jobs(db, threshold_min)
    return {
        "threshold_min": threshold_min,
        "count": len(stuck),
        "jobs": [j.to_dict() for j in stuck],
    }


@router.get("/jobs")
async def list_all_jobs(
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
    limit: int = Query(50, le=500),
    offset: int = Query(0, ge=0),
    status: str = Query(""),
    tenant_id: str = Query(""),
):
    """List all jobs across all tenants. Optional tenant_id filter so the
    admin can drill into a specific customer (e.g. UMG) and watch their
    pipeline live.

    Cada job incluye `username` (outer join con users) — el pipeline del
    admin necesita mostrar QUIÉN creó cada video, no solo el tenant.

    Los jobs de preview de fondo (bg_preview_*) se EXCLUYEN por default:
    son artefactos internos del wizard, no videos del usuario — en el
    pipeline aparecían como "duplicados" de cada video real (confusión
    reportada por el operador 2026-06-02). Para verlos: filtrar por su
    status explícito (?status=bg_preview_done).
    """
    from jobs import _BG_PREVIEW_STATUSES

    query = (
        db.query(Job, User.username)
        .outerjoin(User, Job.user_id == User.id)
        .order_by(Job.created_at.desc())
    )
    if status:
        query = query.filter(Job.status == status)
    else:
        # Sin filtro de status explícito → esconder los previews fantasma.
        query = query.filter(~Job.status.in_(_BG_PREVIEW_STATUSES))
    if tenant_id:
        query = query.filter(Job.tenant_id == tenant_id)

    total = query.count()
    rows = query.offset(offset).limit(limit).all()

    return {
        "total": total,
        "jobs": [{**job.to_dict(), "username": username} for job, username in rows],
    }


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------

@router.get("/invoices")
async def list_all_invoices(
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
):
    """List all invoices across all users."""
    query = db.query(Invoice).order_by(Invoice.created_at.desc())
    total = query.count()
    invoices = query.offset(offset).limit(limit).all()

    return {
        "total": total,
        "invoices": [inv.to_dict() for inv in invoices],
    }


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

@router.get("/audit")
async def list_audit_log(
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
    limit: int = Query(50, le=200),
):
    """View recent audit log entries."""
    entries = (
        db.query(AuditLog)
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": e.id,
            "user_id": e.user_id,
            "action": e.action,
            "detail": e.detail,
            "ip_address": e.ip_address,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in entries
    ]


# ---------------------------------------------------------------------------
# AI Provenance (UMG Compliance)
# ---------------------------------------------------------------------------

@router.get("/cost")
async def admin_cost_dashboard(
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
    since_days: int = Query(30, ge=1, le=365),
):
    """Per-tenant AI cost summary for the last `since_days` days.

    Returns one entry per tenant_id present in the jobs table, ordered by
    spend descending. Use this to spot tenants approaching cap, to validate
    pricing assumptions, and as the data source for cost alerts.
    """
    from provenance import tenant_cost_summary

    tenant_ids = [
        row[0]
        for row in db.query(Job.tenant_id).distinct().all()
    ]

    summaries = []
    grand_total = 0.0
    grand_calls = 0
    for tid in tenant_ids:
        s = tenant_cost_summary(db, tenant_id=tid, since_days=since_days)
        summaries.append(s)
        grand_total += s["total_cost"]
        grand_calls += s["total_calls"]

    summaries.sort(key=lambda s: s["total_cost"], reverse=True)

    return {
        "since_days": since_days,
        "grand_total_cost": round(grand_total, 4),
        "grand_total_calls": grand_calls,
        "tenants": summaries,
    }


@router.get("/margin")
async def admin_margin_dashboard(
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
    since_days: int = Query(30, ge=1, le=365),
    revenue_per_video_usd: float = Query(8.0, ge=0, le=10000),
):
    """Global margin dashboard for the operator. Returns total AI spend,
    per-provider breakdown (veo/gemini/whisper/...), video counts
    (done/pending/rejected/error), cost-per-deliverable and a margin
    estimate against `revenue_per_video_usd` (default $8 reflects the
    current Universal contract: $2,000 / 250 videos). Tighter window =
    fresher signal but noisier, looser window = stable averages."""
    from provenance import cost_dashboard_global
    return cost_dashboard_global(
        db,
        since_days=since_days,
        revenue_per_video_usd=revenue_per_video_usd,
    )


# ---------------------------------------------------------------------------
# Real invoiced cost (billing_sources) + reconciliation vs the model
# ---------------------------------------------------------------------------

def _cost_snapshot_is_usable(period: str, row, billing_sources) -> bool:
    """Whether a stored amount may be treated as a complete invoice.

    An ``ok`` row captured while a month was still accruing is a useful
    checkpoint, but it is not a closed-month invoice.  Keep accepting healthy
    snapshots for an open period (where every total is necessarily
    provisional); for a closed month require the source-specific post-close
    capture boundary used by ``/cost/refresh``.
    """
    if row.status != "ok" or row.amount_usd is None:
        return False
    if period >= billing_sources.current_period():
        return True
    return billing_sources.snapshot_is_final(
        period, row.source, row.fetched_at,
    )

@router.post("/cost/refresh")
def admin_cost_refresh(
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
    period: str = Query("", description="YYYY-MM; vacío = mes actual"),
    only: str = Query("", description="fuentes separadas por coma"),
):
    """Pull real cost from every configured provider and persist it.

    Providers only expose a rolling window (Railway shows the open cycle,
    Replicate paginates predictions that age out), so an un-snapshotted
    month becomes unrecoverable — run this at least once after each month
    closes. Safe to re-run: rows are upserted on (period, source).

    **Deliberately `def`, not `async def`.** `billing_sources` uses blocking
    `requests` calls, and Replicate paginates up to 50 pages at 30 s timeout
    each — worst case several minutes. Inside an `async def` that runs on the
    event loop and stalls EVERY other request the process is serving; with
    2 api replicas that is half the API frozen. FastAPI runs a plain `def`
    endpoint in its threadpool, so the blocking I/O stays off the loop.
    """
    import billing_sources
    from database import CostSnapshot

    period = period.strip() or billing_sources.current_period()
    names = [s.strip() for s in only.split(",") if s.strip()] or None
    try:
        result = billing_sources.fetch_all(period=period, only=names)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    for entry in result["sources"]:
        row = (
            db.query(CostSnapshot)
            .filter(CostSnapshot.period == period,
                    CostSnapshot.source == entry["source"])
            .one_or_none()
        )
        if row is None:
            row = CostSnapshot(period=period, source=entry["source"])
            db.add(row)
        # A healthy *final* closed-month snapshot is immutable. An in-month
        # capture is provisional for usage sources: GCP exports lag and
        # Railway keeps accruing through the boundary, so the first mature
        # post-close refresh must be allowed to replace it. Rolling APIs can
        # later return a superficially successful empty window (Replicate
        # returns ok/$0), hence the source-aware finalization boundary.
        _empty_window_would_erase_spend = (
            row.amount_usd is not None
            and row.amount_usd > 0
            and entry["status"] == "ok"
            and float(entry["amount_usd"] or 0) == 0
            and not entry.get("breakdown")
        )
        if (
            period < billing_sources.current_period()
            and row.status == "ok"
            and row.amount_usd is not None
            and (
                billing_sources.snapshot_is_final(
                    period, entry["source"], row.fetched_at)
                # Rolling provider history (notably Replicate and Railway)
                # may already be empty before the post-close finalization
                # date. Cumulative positive monthly spend cannot legitimately
                # become an exact zero with no line-item evidence.
                or _empty_window_would_erase_spend
            )
        ):
            entry["kept_previous"] = True
            entry["previous_amount_usd"] = row.amount_usd
            entry["discarded_refresh_amount_usd"] = entry["amount_usd"]
            entry["amount_usd"] = row.amount_usd
            entry["status"] = row.status
            entry["detail"] = row.detail
            entry["is_estimate"] = row.is_estimate
            entry["breakdown"] = row.breakdown or []
            continue
        # Un refresh fallido NO pisa un snapshot sano. Las fuentes son
        # ventanas móviles: GitHub sólo puede consultar el ciclo vigente, así
        # que re-refrescar julio en agosto devuelve su error deliberado y
        # borraba el valor bueno capturado cuando julio ERA el mes actual —
        # un dato que ya no se puede volver a pedir. El intento fallido se
        # reporta igual (va en `result`, y queda en `detail`), pero el número
        # sobrevive.
        if (entry["status"] != "ok" and row.status == "ok"
                and row.amount_usd is not None):
            failed_status = entry["status"]
            failed_detail = entry["detail"]
            entry["kept_previous"] = True
            entry["previous_amount_usd"] = row.amount_usd
            entry["discarded_refresh_status"] = failed_status
            entry["discarded_refresh_detail"] = failed_detail
            row.detail = (
                f"{row.detail or ''} | refresh {datetime.now(timezone.utc):%Y-%m-%d}: "
                f"{failed_status} ({failed_detail}) — se conservó el valor anterior"
            ).strip(" |")
            if (
                period == billing_sources.current_period()
                or not billing_sources.snapshot_is_final(
                    period, entry["source"], row.fetched_at,
                )
            ):
                # The saved value is still useful as a checkpoint, but an
                # open-month capture remains provisional even after the
                # calendar rolls over. Only a successful post-close refresh
                # can cross this source's finalization boundary. Do not turn
                # its first failed finalization attempt into a seemingly
                # complete stale total.
                entry["retained_amount_usd"] = row.amount_usd
                entry["amount_usd"] = None
                entry["status"] = failed_status
                entry["detail"] = (
                    f"{failed_detail} — snapshot previo conservado pero stale"
                )
                entry["stale"] = True
                continue
            entry["amount_usd"] = row.amount_usd
            entry["status"] = row.status
            entry["detail"] = row.detail
            entry["is_estimate"] = row.is_estimate
            entry["breakdown"] = row.breakdown or []
            continue
        row.amount_usd = entry["amount_usd"]
        row.status = entry["status"]
        row.detail = entry["detail"]
        row.is_estimate = bool(entry["is_estimate"])
        row.breakdown = entry["breakdown"]
        row.fetched_at = datetime.now(timezone.utc)
    db.commit()
    # `fetch_all` computed its aggregates before immutable/healthy historical
    # rows were restored above. Rebuild every summary field from the final
    # entries so the response cannot say a restored `ok` source is errored or
    # incomplete at the same time.
    configured = [
        entry["source"] for entry in result["sources"]
        if entry["status"] == "ok" and entry["amount_usd"] is not None
    ]
    not_configured = [
        entry["source"] for entry in result["sources"]
        if entry["status"] == "not_configured"
    ]
    errored = [
        entry["source"] for entry in result["sources"]
        if entry["status"] not in ("ok", "not_configured")
        or (entry["status"] == "ok" and entry["amount_usd"] is None)
    ]
    # `only=` is an operational refresh scope, never proof that the monthly
    # total is complete. Even a healthy `only=fixed` response omits every
    # provider API and must remain explicitly partial/non-quoteable.
    not_requested = (
        [source for source in billing_sources.SOURCES if source not in names]
        if names is not None else []
    )
    result.update({
        "total_usd": round(sum(
            float(entry["amount_usd"])
            for entry in result["sources"]
            if entry["status"] == "ok" and entry["amount_usd"] is not None
        ), 2),
        "configured": configured,
        "not_configured": not_configured,
        "errored": errored,
        "not_requested": not_requested,
        "partial": bool(not_requested),
        "complete": not not_configured and not errored and not not_requested,
    })

    return result


@router.get("/cost/real")
def admin_cost_real(          # `def`, no `async def`: con live=true hace HTTP
    admin: dict = Depends(require_admin),   # bloqueante (ver /cost/refresh).
    db: Session = Depends(get_db),
    period: str = Query("", description="YYYY-MM; vacío = mes actual"),
    live: bool = Query(False, description="consultar las APIs en vez de leer el snapshot"),
):
    """Invoiced cost for a month, per provider.

    Reads the persisted snapshot by default (fast, and the only option for
    closed months). `live=true` re-queries the provider APIs without
    writing anything — use it to sanity-check before refreshing.

    `complete=false` means at least one source is unconfigured or errored,
    so `total_usd` is a floor, not the real total. Don't divide by video
    count and quote the result until it's true.
    """
    import billing_sources
    from database import CostSnapshot

    period = period.strip() or billing_sources.current_period()
    try:
        billing_sources._period_bounds(period)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if live:
        return {**billing_sources.fetch_all(period=period), "source_of_truth": "live"}

    # `rate_calibration` guarda tarifas, no gasto: su `amount_usd` es NULL a
    # propósito. Contarla como una fuente de facturación la mandaba al balde
    # de "errored" y dejaba `complete=false` para siempre, escondiendo si
    # faltaba de verdad alguna factura.
    rows = (
        db.query(CostSnapshot)
        .filter(CostSnapshot.period == period)
        .filter(CostSnapshot.source.in_(tuple(billing_sources.SOURCES)))
        .all()
    )
    if not rows:
        return {
            "period": period, "total_usd": 0.0, "sources": [],
            "configured": [], "not_configured": list(billing_sources.SOURCES),
            "errored": [], "complete": False, "source_of_truth": "snapshot",
            "detail": "sin snapshot para este período — corré POST /admin/cost/refresh",
        }

    sources, total, ok, missing, errored = [], 0.0, [], [], []
    for r in rows:
        usable = _cost_snapshot_is_usable(period, r, billing_sources)
        source = {
            "source": r.source, "period": r.period, "amount_usd": r.amount_usd,
            "status": r.status, "detail": r.detail,
            "is_estimate": r.is_estimate, "breakdown": r.breakdown or [],
            "fetched_at": r.fetched_at.isoformat() if r.fetched_at else None,
        }
        if usable:
            total += r.amount_usd
            ok.append(r.source)
        elif r.status == "ok" and r.amount_usd is not None:
            # Preserve the checkpoint for diagnostics without letting callers
            # sum or quote it as a completed invoice.
            source.update({
                "amount_usd": None,
                "status": "provisional",
                "retained_amount_usd": r.amount_usd,
                "stale": True,
                "detail": (
                    f"{r.detail or 'snapshot'} — captura previa al cierre; "
                    "requiere refresh post-cierre"
                ),
            })
            errored.append(r.source)
        elif r.status == "not_configured":
            missing.append(r.source)
        else:
            errored.append(r.source)
        sources.append(source)
    missing += [s for s in billing_sources.SOURCES
                if s not in {r.source for r in rows}]
    sources.sort(key=lambda s: -(
        s["amount_usd"] or s.get("retained_amount_usd") or 0
    ))

    return {
        "period": period, "total_usd": round(total, 2), "sources": sources,
        "configured": ok, "not_configured": missing, "errored": errored,
        "complete": not missing and not errored,
        "source_of_truth": "snapshot",
    }


@router.get("/cost/unit-economics")
def admin_cost_unit_economics(
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
    period: str = Query("", description="YYYY-MM; vacío = mes actual"),
    price_per_video_usd: float = Query(13.50, ge=0, le=10000),
):
    """The number to price against: real invoiced cost ÷ videos delivered.

    This is the endpoint the 2026-08 audit was built for. Two traps it
    closes:

    1. **Denominator.** Cost is divided by videos actually DELIVERED
       (done + pending_review), never by jobs created. Dividing $199.53 of
       jun-2026 Google Cloud by 173 created jobs gives $1.15/video; by the
       65 that shipped it is $3.07. The second one is real — the discarded
       previews cost money too.
    2. **Completeness.** `cost_complete` propagates from /cost/real. When
       false, `cost_per_delivered` is a floor and is labelled as such.
    """
    import billing_sources
    from database import CostSnapshot
    from provenance import (
        cost_waste_breakdown,
        delivered_job_filter,
        merge_waste_breakdowns,
        rates_for_window,
    )

    period = period.strip() or billing_sources.current_period()
    try:
        start, end = billing_sources._period_bounds(period)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Igual que en /cost/real: `rate_calibration` no es una fuente de gasto.
    rows = (db.query(CostSnapshot)
              .filter(CostSnapshot.period == period)
              .filter(CostSnapshot.source.in_(tuple(billing_sources.SOURCES)))
              .all())
    usable_rows = [
        r for r in rows
        if _cost_snapshot_is_usable(period, r, billing_sources)
    ]
    real_total = sum(r.amount_usd for r in usable_rows)
    have = {
        r.source for r in usable_rows
    }
    cost_complete = have >= set(billing_sources.SOURCES)

    start_dt = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    next_day = end + timedelta(days=1)
    end_dt = datetime(next_day.year, next_day.month, next_day.day,
                      tzinfo=timezone.utc)
    # The invoice covers BOTH environments (shared GCP project, R2 bucket
    # and Railway project), so the denominator has to as well. Counting
    # only the local environment would divide two-env spend by one-env
    # output — and since managed UMG production runs in staging, on prod
    # that inflates cost-per-video by roughly an order of magnitude.
    from database import scoped_peer_db

    def _count(session):
        # Los ENTREGADOS se cuentan por `completed_at`, no por `created_at`:
        # una canción arrancada el 30 de junio y terminada el 2 de julio
        # pertenece al costo de julio, que es cuando se gastó y cuando se
        # factura. `coalesce` cubre filas viejas sin el campo.
        entregado_en = func.coalesce(Job.completed_at, Job.created_at)
        # During an edit the current status is transiently `editing` (or may
        # finish as `error`), but a retained completion timestamp older than
        # editing_started_at proves the video had already shipped. A rejected
        # job reopened by the fixed /edit path clears completed_at first; if
        # that rescue fails, its new failure timestamp is after editing_started_at
        # and therefore does not masquerade as a delivery.
        delivered = int(
            session.query(func.count(Job.id))
            .filter(entregado_en >= start_dt, entregado_en < end_dt)
            .filter(delivered_job_filter())
            .scalar() or 0
        )
        # Los CREADOS sí van por created_at — es literalmente eso lo que mide.
        created = int(
            session.query(func.count(Job.id))
            .filter(Job.created_at >= start_dt, Job.created_at < end_dt)
            .scalar() or 0
        )
        return delivered, created

    # Ventana EXPLÍCITA del mes pedido. Con `since_days` la ventana termina
    # hoy, así que pedir junio en agosto medía jul-ago y lo comparaba contra
    # la factura de junio.
    delivered, created = _count(db)
    counted_environments = 1
    # Una sola base de valuación para los dos entornos: la calibración vive
    # en la base local y la peer no la tiene, así que dejarla cargar la suya
    # valuaba su mitad a precio de lista y el `waste_ratio` mezclado salía de
    # dos tarifas distintas para el mismo Veo.
    _waste_rates = rates_for_window(db, start_dt)
    waste_parts = [cost_waste_breakdown(db, start=start_dt, end=end_dt,
                                        rates=_waste_rates)]
    with scoped_peer_db() as peer:
        if peer is not None:
            d2, c2 = _count(peer)
            delivered += d2
            created += c2
            counted_environments = 2
            # El desperdicio también sale de los DOS entornos: la producción
            # gestionada de UMG corre en staging, así que un subárbol local
            # al lado de totales de dos entornos describía una porción del
            # negocio con cara de describirlo entero.
            waste_parts.append(
                cost_waste_breakdown(peer, start=start_dt, end=end_dt,
                                     rates=_waste_rates))
    waste = merge_waste_breakdowns(*waste_parts)

    cost_per_delivered = round(real_total / delivered, 4) if delivered else None
    # Kept only to show the operator how misleading it is next to the real
    # one — it is the number the old internal doc quoted.
    cost_per_created = round(real_total / created, 4) if created else None

    return {
        "period": period,
        "real_cost_usd": round(real_total, 2),
        "cost_complete": cost_complete,
        "missing_sources": sorted(set(billing_sources.SOURCES) - have),
        "videos_delivered": delivered,
        "videos_created": created,
        # 1 = only this environment was counted while the invoice covers
        # both; the cost per video is then overstated. Check before quoting.
        "counted_environments": counted_environments,
        "cost_per_delivered": cost_per_delivered,
        "cost_per_created_MISLEADING": cost_per_created,
        "price_per_video_usd": price_per_video_usd,
        "margin_per_video": (
            round(price_per_video_usd - cost_per_delivered, 4)
            if cost_per_delivered is not None else None
        ),
        "margin_pct": (
            round((price_per_video_usd - cost_per_delivered)
                  / price_per_video_usd, 4)
            if cost_per_delivered is not None and price_per_video_usd else None
        ),
        "waste": waste,
        "note": (
            "cost_per_delivered usa SOLO videos entregados como denominador. "
            "Si cost_complete=false, el número es un piso."
        ),
    }


@router.get("/cost/reconcile")
async def admin_cost_reconcile(
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
    period: str = Query("", description="YYYY-MM; vacío = mes actual"),
):
    """Modeled AI cost (provenance × rate table) vs what the provider billed.

    The rate table in provenance.py is an estimate. This is how we know
    whether it still holds: jun-2026 reconciled at $163 modeled vs $199.53
    invoiced (-18%), the gap being staging usage plus real Imagen/Gemini
    token pricing. If `variance_pct` drifts much beyond that, the rates
    need recalibrating — a silently-wrong rate table is worse than none,
    because per-tenant cost attribution inherits the error.

    Only compares AI providers (gcp/openai/replicate); Railway, R2 and the
    flat subscriptions never flow through `record_ai_call`, so the model
    has nothing to say about them.
    """
    import billing_sources
    from database import CostSnapshot
    from provenance import cost_for_record

    period = period.strip() or billing_sources.current_period()
    try:
        start, end = billing_sources._period_bounds(period)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    AI_SOURCES = ("gcp", "openai", "replicate")
    rows = (
        db.query(CostSnapshot)
        .filter(CostSnapshot.period == period,
                CostSnapshot.source.in_(AI_SOURCES))
        .all()
    )
    import cost_attribution as ca

    invoiced = 0.0
    for row in rows:
        if not _cost_snapshot_is_usable(period, row, billing_sources):
            continue
        amount = float(row.amount_usd)
        if row.source == "gcp":
            amount, _shared = ca.split_gcp_invoice(
                amount, row.breakdown or [])
        invoiced += amount
    have = {
        r.source for r in rows
        if _cost_snapshot_is_usable(period, r, billing_sources)
    }

    # `cost_dashboard_global` measures a window ending NOW, so asking it
    # for "30 days" while comparing against June's invoice compared two
    # disjoint months. Sum the calendar period directly instead — the
    # whole point of this endpoint is that the two sides line up, and the
    # note tells the operator to recalibrate the rate table from the
    # result.
    from provenance import billable_filter
    from database import AIProvenance

    start_dt = datetime(start.year, start.month, start.day,
                        tzinfo=timezone.utc)
    next_day = end + timedelta(days=1)
    end_dt = datetime(next_day.year, next_day.month, next_day.day,
                      tzinfo=timezone.utc)

    def _modeled(session):
        rows = (
            session.query(AIProvenance.tool_name, AIProvenance.tool_provider,
                          func.count(AIProvenance.id))
            .filter(AIProvenance.created_at >= start_dt,
                    AIProvenance.created_at < end_dt)
            .filter(billable_filter())
            .group_by(AIProvenance.tool_name, AIProvenance.tool_provider)
            .all()
        )
        total = 0.0
        by_tool = []
        for tool_name, tool_provider, calls in rows:
            cost = calls * cost_for_record(tool_name, tool_provider)
            total += cost
            by_tool.append({"tool_name": tool_name,
                            "tool_provider": tool_provider,
                            "calls": calls, "cost": round(cost, 4)})
        return total, by_tool

    modeled, by_tool = _modeled(db)
    # The invoice is for the shared GCP project, i.e. both environments.
    from database import scoped_peer_db
    counted_environments = 1
    with scoped_peer_db() as peer:
        if peer is not None:
            peer_total, peer_tools = _modeled(peer)
            modeled += peer_total
            by_tool += peer_tools
            counted_environments = 2
    by_tool.sort(key=lambda r: -r["cost"])

    variance = invoiced - modeled
    return {
        "period": period,
        "modeled_usd": round(modeled, 2),
        "invoiced_usd": round(invoiced, 2),
        "variance_usd": round(variance, 2),
        "variance_pct": (round(variance / modeled, 4) if modeled else None),
        "invoiced_sources_present": sorted(have),
        "invoiced_sources_missing": sorted(set(AI_SOURCES) - have),
        "calibration_factor": (
            round(invoiced / modeled, 4) if modeled else None
        ),
        "by_tool_modeled": by_tool,
        "counted_environments": counted_environments,
        "note": (
            "calibration_factor >1 = el modelo subestima. Multiplicá las "
            "tarifas de COST_PER_CALL por este factor para recalibrar. "
            "Ambos lados cubren el MISMO mes calendario. Si "
            "counted_environments=1 el modelo mide un solo entorno "
            "mientras la factura cubre los dos (proyecto GCP compartido), "
            "así que el factor va a salir alto: configurá PEER_DATABASE_URL "
            "antes de recalibrar nada."
        ),
    }


def _run_attribution(db, period: str | None):
    """Shared helper for the attribution endpoints.

    Collects from the local environment plus the peer one (staging↔prod)
    when `PEER_DATABASE_URL` is configured. Managed UMG production runs in
    staging under team accounts while Universal's self-service runs in
    prod, so a single-environment answer is always partial — the caller
    gets `environments` and `single_environment` so it can say so instead
    of quietly under-reporting.
    """
    import cost_attribution as ca
    from database import PEER_DATABASE_URL, scoped_deliveries_db, scoped_peer_db

    if period:
        try:
            ca.period_bounds(period)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    with scoped_peer_db() as peer:
        sessions = {"local": db}
        if peer is not None:
            sessions["peer"] = peer

        # The deliveries portal is prod-backed; `scoped_deliveries_db` already
        # routes there and guarantees the session is returned to the pool.
        with scoped_deliveries_db() as portal_db:
            portal = ca.collect_portal_songs(portal_db)

        # Una sola calibración para TODOS los entornos. La leemos de `db`
        # porque es la base contra la que se corre `/cost/calibrate-rates`;
        # la peer es de sólo lectura y no tiene el snapshot, así que dejarla
        # cargar la suya valuaba las mismas llamadas a precio de lista y el
        # informe mezclaba dos tarifas para el mismo Veo.
        rates = {}
        if period:
            from rate_calibration import load_applied_rates
            rates = load_applied_rates(db, period)

        jobs_by_env = {
            env: ca.collect_jobs(s, env, period=period, rates=rates)
            for env, s in sessions.items()
        }
        all_time_keys: set[str] = set()
        if period:
            for s in sessions.values():
                all_time_keys |= ca.collect_song_keys(s)

        result = ca.build_attribution(
            jobs_by_env, portal, period=period,
            all_time_song_keys=all_time_keys if period else None,
        )
        result["single_environment"] = peer is None
        if peer is None:
            result["warning"] = (
                "PEER_DATABASE_URL no configurada: solo se midió este "
                "entorno. La producción gestionada de UMG corre en STAGING, "
                "así que el costo puede quedar muy subestimado."
            )
        result["peer_configured"] = bool(PEER_DATABASE_URL)
        return result


@router.get("/cost/umg")
def admin_cost_umg(
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
    period: str = Query("", description="YYYY-MM; vacío = todo el histórico"),
    revenue_usd: float = Query(0.0, ge=0, le=1_000_000,
                               description="ingreso del período, para el margen"),
    basis: str = Query("cost", pattern="^(cost|jobs)$"),
    top: int = Query(25, ge=1, le=500),
):
    """Costo real por canción entregada a Universal (niveles 1 y 2).

    Nivel 1 es el costo directo de IA de las canciones que UMG pidió —
    las del portal `umg.genly.pro` más las de los tenants `universal_*` —
    contando TODOS los jobs de cada canción (variantes, re-renders,
    ediciones), que es lo que realmente se pagó por entregarla.

    Nivel 2 suma la parte prorrateada de infraestructura compartida, y sale
    solo si hay snapshots de facturación del período (POST /admin/cost/refresh).

    El denominador es la CANCIÓN, no el job: un entregable lleva ~2,4 jobs,
    así que el costo por job subestima ~2,4x lo que cuesta entregar.
    """
    import billing_sources
    from database import CostSnapshot

    period = period.strip() or None
    result = _run_attribution(db, period)

    # Trim the per-song detail; the full list is large and the caller
    # almost always wants the expensive tail.
    songs = result["umg"]["by_song"]
    result["umg"]["by_song_truncated"] = len(songs) > top
    result["umg"]["by_song"] = songs[:top]

    # Level 2 needs the real invoices for the period.
    if period:
        rows = (
            db.query(CostSnapshot)
            .filter(CostSnapshot.period == period,
                    CostSnapshot.source.in_(tuple(billing_sources.SOURCES)))
            .all()
        )
        usable_rows = [
            r for r in rows
            if _cost_snapshot_is_usable(period, r, billing_sources)
        ]
        invoices = {r.source: r.amount_usd for r in usable_rows}
        invoice_breakdowns = {
            r.source: (r.breakdown or []) for r in usable_rows
        }
        if invoices:
            import cost_attribution as ca
            ca.add_total_cost(
                result, invoices,
                revenue_usd=revenue_usd or None, basis=basis,
                invoice_breakdowns=invoice_breakdowns,
            )
            missing = sorted(set(billing_sources.SOURCES) - set(invoices))
            result["umg_total"]["invoices_missing_sources"] = missing
            result["umg_total"]["invoices_complete"] = not missing
        else:
            result["umg_total_unavailable"] = (
                f"sin snapshots de facturación para {period} — corré "
                f"POST /admin/cost/refresh?period={period}"
            )
    return result


@router.post("/cost/calibrate-rates")
def admin_calibrate_rates(    # `def`: consulta BigQuery, que bloquea hasta
    admin: dict = Depends(require_admin),   # BILLING_HTTP_TIMEOUT segundos.
    db: Session = Depends(get_db),
    period: str = Query("", description="YYYY-MM; vacío = mes actual"),
    dry_run: bool = Query(False, description="calcular sin guardar"),
):
    """Deriva la tarifa REAL por llamada desde la factura y la persiste.

    Ningún proveedor de IA devuelve el costo en la respuesta, así que la
    única fuente real es la factura:

        tarifa real = costo facturado del SKU ÷ llamadas facturables medidas

    Medido: Veo estaba cargado a $0,80 de lista y la factura da ~$0,62 —
    el panel sobreestimaba ~25%, y ese error se propagaba al costo por
    canción, al margen por tenant y al tamaño del desperdicio.

    Requiere `PEER_DATABASE_URL`: staging y prod comparten el proyecto de
    GCP, así que la factura cubre los dos entornos y contar uno solo
    duplicaría la tarifa. Sin peer, se niega a calibrar en vez de guardar
    un número mal.

    Necesita también el export de facturación a BigQuery configurado
    (`GCP_BILLING_BQ_*`) — es el único lugar con granularidad de SKU.
    """
    import billing_sources
    import rate_calibration as rc
    from database import PEER_DATABASE_URL, scoped_peer_db

    period = period.strip() or billing_sources.current_period()
    try:
        rc_period_check = billing_sources._period_bounds(period)
        del rc_period_check
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # GCP's export lags the usage it describes. A current/open month (or a
    # just-closed month still inside that lag) can be useful as a preview but
    # must never overwrite the estimated rate used by dashboards.
    rates_are_final = billing_sources.snapshot_is_final(
        period, "gcp", datetime.now(timezone.utc),
    )

    gcp = billing_sources.fetch_gcp(period)
    if gcp.status != "ok":
        return {
            "period": period, "calibrated": False,
            "reason": f"sin factura de GCP utilizable: {gcp.detail}",
            "hint": ("configurá el export de facturación a BigQuery "
                     "(GCP_BILLING_BQ_*). No es retroactivo."),
        }

    invoiced = billing_sources.gcp_cost_by_tool(gcp)

    with scoped_peer_db() as peer:
        if peer is None:
            return {
                "period": period, "calibrated": False,
                "reason": ("falta PEER_DATABASE_URL. La factura de GCP cubre "
                           "staging Y prod (mismo proyecto); calibrar con un "
                           "solo entorno duplicaría la tarifa."),
                "invoiced_by_tool": invoiced,
            }
        sessions = {"local": db, "peer": peer}
        result = rc.derive_rates(sessions, invoiced, period)

    result["invoiced_by_tool"] = invoiced
    result["gcp_total_usd"] = gcp.amount_usd
    if not rates_are_final:
        result["provisional"] = True
        result["provisional_applied"] = dict(result.get("applied") or {})
        result["applied"] = {}
        result["stored"] = False
        result["reason"] = (
            "mes abierto o dentro del rezago de finalización de GCP; "
            "las tarifas se muestran como provisionales y no se aplican"
        )
    elif not dry_run:
        result["stored"] = rc.store_rates(db, period, result)
        result["provisional"] = False
    else:
        result["stored"] = False
        result["provisional"] = False
    result["calibrated"] = bool(result.get("applied"))
    return result


@router.get("/cost/rates")
async def admin_cost_rates(
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
    period: str = Query("", description="YYYY-MM; vacío = mes actual"),
):
    """Qué tarifa está usando el panel para cada herramienta, y de dónde sale.

    `source: "factura"` = derivada del gasto real de ese mes.
    `source: "estimada"` = tarifa de lista de COST_PER_CALL, porque todavía
    no hay factura calibrada. La distinción importa: la estimada de Veo
    venía ~25% alta.
    """
    import billing_sources
    import rate_calibration as rc
    from provenance import COST_PER_CALL

    period = period.strip() or billing_sources.current_period()
    calibrated = rc.load_applied_rates(db, period)

    rows = []
    for (name, provider), estimated in sorted(COST_PER_CALL.items()):
        real = rc.rate_for_tool(name, calibrated)
        rows.append({
            "tool_name": name, "provider": provider,
            "rate_in_use": real if real is not None else estimated,
            "source": "factura" if real is not None else "estimada",
            "estimated_rate": estimated,
            "calibrated_rate": real,
            "drift": (round(real / estimated, 3)
                      if real is not None and estimated else None),
        })
    rows.sort(key=lambda r: -r["rate_in_use"])
    return {
        "period": period,
        "calibrated_tools": sorted(calibrated),
        "rates": rows,
        "note": ("'estimada' es tarifa de lista y puede desviarse: Veo "
                 "estaba a $0,80 y la factura da ~$0,62. Corré "
                 f"POST /admin/cost/calibrate-rates?period={period}"),
    }


@router.get("/quality/change-requests")
async def admin_change_requests(
    admin: dict = Depends(require_admin),
    period: str = Query("", description="YYYY-MM; vacío = todo el histórico"),
    include_raw: bool = Query(False, description="devolver los comentarios crudos"),
):
    """Qué nos pide cambiar el cliente, clasificado.

    La única medición directa de calidad que existe: todo lo demás (ediciones
    del operador, tasa de rechazo) mide trabajo interno, que es un supuesto
    sobre lo que el cliente quiere. Esto es lo que el cliente escribió.

    `requests_per_delivery` es el indicador a seguir mes a mes — el retrabajo
    es la línea de costo que define el margen del contrato llave en mano.

    La tabla vive en la base de PROD (el portal es prod-backed) aunque la
    producción gestionada corra en staging; `get_deliveries_db` ya rutea bien
    desde los dos entornos.
    """
    import change_request_stats as crs
    from database import Delivery, DeliveryChangeRequest, scoped_deliveries_db

    with scoped_deliveries_db() as ddb:
        q = ddb.query(DeliveryChangeRequest)
        dq = ddb.query(func.count(Delivery.id)).filter(
            Delivery.removed_at.is_(None))
        if period.strip():
            try:
                start, end = _month_bounds(period.strip())
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            q = q.filter(DeliveryChangeRequest.submitted_at >= start,
                         DeliveryChangeRequest.submitted_at < end)
            dq = dq.filter(Delivery.added_at >= start, Delivery.added_at < end)

        rows = q.order_by(DeliveryChangeRequest.submitted_at).all()
        result = crs.summarize(rows, deliveries_total=int(dq.scalar() or 0))
        result["period"] = period.strip() or None
        if include_raw:
            result["raw"] = [
                {"id": r.id, "comment": r.comment,
                 "submitted_at": r.submitted_at.isoformat() if r.submitted_at else None,
                 "resolved": r.resolved_at is not None,
                 "categories": crs.classify(r.comment or ""),
                 "noise": crs.is_noise(r.comment or "")}
                for r in rows
            ]
        return result


def _month_bounds(period: str):
    """"YYYY-MM" -> (inicio, fin exclusivo) en UTC."""
    year, month = (int(x) for x in period.split("-", 1))
    if not 1 <= month <= 12:
        raise ValueError(f"período inválido: {period!r}")
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    end = datetime(year + (month == 12), (month % 12) + 1, 1,
                   tzinfo=timezone.utc)
    return start, end


@router.get("/cost/business")
def admin_cost_business(
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
    period: str = Query("", description="YYYY-MM; vacío = todo el histórico"),
):
    """A qué se fue cada dólar del negocio (nivel 3).

    Clasifica TODO el gasto de ambos entornos: producción de UMG, otros
    clientes, automatización/CI (golden_render_bot, smokes, e2e) e I+D
    interno. Sirve para separar costo de bienes vendidos de costo de
    operar — lo segundo no se le carga al precio del cliente.

    El orden de clasificación importa y está fijado por tests: un job del
    render bot que reprocesa una canción real del catálogo cuenta como CI,
    nunca como producción del cliente.
    """
    result = _run_attribution(db, period.strip() or None)
    # The per-song detail belongs to /cost/umg; keep this response about
    # the business-wide split.
    result["umg"].pop("by_song", None)
    return result


# NOTE: this catch-all must stay AFTER every literal /cost/<name> route.
# FastAPI matches in registration order, so declaring it earlier made
# `/admin/cost/real` resolve here with tenant_id="real" — a 200 with an
# empty cost summary instead of the real-invoice payload.
@router.get("/cost/{tenant_id}")
async def admin_tenant_cost(
    tenant_id: str,
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
    since_days: int = Query(30, ge=1, le=365),
):
    """Cost summary for a single tenant, broken down by tool."""
    from provenance import tenant_cost_summary
    return tenant_cost_summary(db, tenant_id=tenant_id, since_days=since_days)


@router.get("/provenance")
async def list_all_provenance(
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
    limit: int = Query(50, le=500),
    offset: int = Query(0, ge=0),
    job_id: str = Query(""),
    tool_name: str = Query(""),
):
    """List AI provenance records across all jobs."""
    query = db.query(AIProvenance).order_by(AIProvenance.created_at.desc())
    if job_id:
        query = query.filter(AIProvenance.job_id == job_id)
    if tool_name:
        query = query.filter(AIProvenance.tool_name.ilike(f"%{tool_name}%"))

    total = query.count()
    records = query.offset(offset).limit(limit).all()

    return {
        "total": total,
        "records": [
            {
                "id": r.id,
                "job_id": r.job_id,
                "step": r.step,
                "tool_name": r.tool_name,
                "tool_provider": r.tool_provider,
                "input_data_types": r.input_data_types,
                "duration_ms": r.duration_ms,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in records
        ],
    }


# ---------------------------------------------------------------------------
# Per-user activity observability
# ---------------------------------------------------------------------------

# Job statuses that count as "still moving through the pipeline" for the
# activity rollup. Mirrors the status set used by the dashboard; keep in
# sync if new intermediate statuses are added.
_ACTIVITY_IN_PROGRESS_STATUSES = (
    "processing", "queued", "transcribed_pending", "transcribed",
    "pending_review", "editing", "awaiting_upload",
)
_ACTIVITY_FAILED_STATUSES = ("error", "validation_failed")

# AuditLog actions that represent rework: the user (or reviewer) had to go
# back over something the pipeline already produced. Used by /activity to
# quantify friction per user without new tracking.
_ACTIVITY_EDIT_ACTION = "job.edit_request"
_ACTIVITY_RETRY_ACTION = "job.retry"
_ACTIVITY_SEGMENTS_ACTION = "lyrics.segments_diff"
_ACTIVITY_DOWNLOAD_ACTION = "job.download"


def _activity_window(since_days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=since_days)


@router.get("/activity")
async def admin_activity(
    admin: dict = Depends(require_super_admin),
    db: Session = Depends(get_db),
    since_days: int = Query(30, ge=1, le=365),
):
    """Per-user activity rollup across all tenants.

    One row per user with everything the operator needs to answer "who is
    actually using the app and how is it going for them": video counts by
    outcome, errors, rework signals (edits / retries / variants /
    re-created jobs), background source split (library vs AI-generated)
    and estimated AI cost. All derived from existing tables — Job,
    AuditLog, AssetUsage, AIProvenance — no extra tracking.
    """
    from provenance import cost_for_record

    since = _activity_window(since_days)

    # Excluir jobs de preview de fondo (bg_preview_*): son auxiliares del
    # wizard, no videos que el usuario haya pedido.
    real_jobs = ~Job.status.like("bg_preview%")

    # --- 1. Video counts + last job activity, one GROUP BY ---------------
    video_rows = (
        db.query(
            Job.user_id,
            func.count(Job.id).label("total"),
            func.sum(case((Job.status == "done", 1), else_=0)).label("done"),
            # Aprobado = aprobado Y todavía en done. Un video aprobado que
            # después volvió a edición ya no cuenta — si no, "aprobados"
            # puede superar a "terminados" y confunde (visto en staging).
            func.sum(case(((Job.approved_at.isnot(None)) & (Job.status == "done"), 1), else_=0)).label("approved"),
            func.sum(case((Job.status.in_(_ACTIVITY_FAILED_STATUSES), 1), else_=0)).label("failed"),
            func.sum(case((Job.status.in_(_ACTIVITY_IN_PROGRESS_STATUSES), 1), else_=0)).label("in_progress"),
            func.max(func.coalesce(Job.last_user_activity_at, Job.created_at)).label("last_job_activity"),
        )
        .filter(Job.created_at >= since, real_jobs)
        .group_by(Job.user_id)
        .all()
    )
    videos_by_user = {}
    last_activity_by_user = {}
    for r in video_rows:
        videos_by_user[r.user_id] = {
            "total": int(r.total or 0),
            "done": int(r.done or 0),
            "approved": int(r.approved or 0),
            "failed": int(r.failed or 0),
            "in_progress": int(r.in_progress or 0),
        }
        last_activity_by_user[r.user_id] = r.last_job_activity

    # --- 2. Recent error messages (bounded), bucketed per user -----------
    # También clasifica cada error en categoría (veo/render/upload/...):
    # usa la columna error_category cuando el pipeline la pobló, y cae al
    # clasificador de texto para rows históricas. El límite de 200 cubre
    # de sobra el volumen de errores de una ventana de 90 días al volumen
    # actual; si algún día se queda corto, la categoría sub-reporta pero
    # nunca rompe.
    error_rows = (
        db.query(Job.user_id, Job.job_id, Job.song_title, Job.artist, Job.error,
                 Job.error_category, Job.status, Job.created_at)
        .filter(Job.status.in_(_ACTIVITY_FAILED_STATUSES), Job.created_at >= since)
        .order_by(Job.created_at.desc())
        .limit(200)
        .all()
    )
    errors_by_user = {}
    error_categories_by_user = {}
    errors_by_category_global = {}
    for r in error_rows:
        category = r.error_category or classify_error(r.error)
        cat_bucket = error_categories_by_user.setdefault(r.user_id, {})
        cat_bucket[category] = cat_bucket.get(category, 0) + 1
        errors_by_category_global[category] = errors_by_category_global.get(category, 0) + 1
        bucket = errors_by_user.setdefault(r.user_id, [])
        if len(bucket) < 3:
            bucket.append({
                "job_id": r.job_id,
                "artist": r.artist,
                "song_title": r.song_title,
                "status": r.status,
                "error": (r.error or "")[:300],
                "category": category,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            })

    # --- 3. Rework from Job columns: variants + partial re-renders -------
    rework_rows = (
        db.query(
            Job.user_id,
            func.sum(case((Job.parent_job_id.isnot(None), 1), else_=0)).label("variants"),
            func.sum(case((Job.edit_count > 0, 1), else_=0)).label("rerendered_jobs"),
            func.sum(Job.edit_count).label("total_edits"),
        )
        .filter(Job.created_at >= since, real_jobs)
        .group_by(Job.user_id)
        .all()
    )
    rework_by_user = {
        r.user_id: {
            "variants": int(r.variants or 0),
            "rerendered_jobs": int(r.rerendered_jobs or 0),
            "total_edits": int(r.total_edits or 0),
        }
        for r in rework_rows
    }

    # --- 4. Rework from AuditLog: edit types, retries, manual lyric edits.
    # El detail es JSONB; lo leemos en Python (y no con operadores JSON de
    # SQL) para que el mismo código corra igual en SQLite (tests locales)
    # y Postgres (CI / prod).
    audit_rows = (
        db.query(AuditLog.user_id, AuditLog.action, AuditLog.detail)
        .filter(
            AuditLog.action.in_((
                _ACTIVITY_EDIT_ACTION,
                _ACTIVITY_RETRY_ACTION,
                _ACTIVITY_SEGMENTS_ACTION,
            )),
            AuditLog.created_at >= since,
        )
        .all()
    )
    audit_rework_by_user = {}
    for user_id, action, detail in audit_rows:
        if user_id is None:
            continue
        agg = audit_rework_by_user.setdefault(user_id, {
            "edits_lyrics": 0, "edits_typography": 0, "edits_background": 0,
            "edits_metadata": 0, "retries": 0, "corrected_jobs": set(),
        })
        if action == _ACTIVITY_RETRY_ACTION:
            agg["retries"] += 1
        elif action == _ACTIVITY_SEGMENTS_ACTION:
            # Correcciones manuales de letra: contamos JOBS distintos, no
            # eventos. El editor de letras autoguarda (un evento
            # lyrics.segments_diff por save) → contar eventos infla el
            # número a miles y lo vuelve inútil (bug visto en staging
            # 2026-06-02: "Retrabajos: 1011"). La pregunta que responde
            # esta métrica es "¿cuántos videos necesitaron corrección
            # manual?", no "¿cuántas veces se guardó?".
            agg["corrected_jobs"].add((detail or {}).get("job_id") or "?")
        elif action == _ACTIVITY_EDIT_ACTION:
            edit_type = ((detail or {}).get("edit_type") or "").strip()
            key = f"edits_{edit_type}"
            if key in agg:
                agg[key] += 1
    # set de job_ids → count para serializar a JSON
    for agg in audit_rework_by_user.values():
        agg["corrected_jobs"] = len(agg["corrected_jobs"])

    # --- 5. Abandoned-and-recreated heuristic -----------------------------
    # Mismo usuario, misma (artist, song_title), más de un job y al menos
    # uno que nunca llegó a done → señal de que descartó un intento y
    # arrancó de nuevo.
    recreated_rows = (
        db.query(
            Job.user_id,
            func.count(Job.id).label("n"),
            func.sum(case((Job.status == "done", 1), else_=0)).label("done_n"),
        )
        .filter(Job.created_at >= since, real_jobs, Job.parent_job_id.is_(None))
        .group_by(Job.user_id, Job.artist, Job.song_title)
        .having(func.count(Job.id) > 1)
        .all()
    )
    recreated_by_user = {}
    for r in recreated_rows:
        if int(r.n or 0) > int(r.done_n or 0):
            recreated_by_user[r.user_id] = recreated_by_user.get(r.user_id, 0) + 1

    # --- 6. Backgrounds: library usage ------------------------------------
    library_rows = (
        db.query(AssetUsage.user_id, func.count(AssetUsage.id))
        .filter(AssetUsage.used_at >= since)
        .group_by(AssetUsage.user_id)
        .all()
    )
    library_by_user = {uid: int(n or 0) for uid, n in library_rows}

    # --- 7. Backgrounds AI-generated + AI cost, via provenance ------------
    prov_rows = (
        db.query(
            Job.user_id,
            AIProvenance.tool_name,
            AIProvenance.tool_provider,
            AIProvenance.step,
            func.count(AIProvenance.id).label("calls"),
        )
        .join(Job, Job.job_id == AIProvenance.job_id)
        .filter(AIProvenance.created_at >= since)
        .group_by(Job.user_id, AIProvenance.tool_name, AIProvenance.tool_provider, AIProvenance.step)
        .all()
    )
    ai_bg_by_user = {}
    ai_cost_by_user = {}
    for user_id, tool_name, tool_provider, step, calls in prov_rows:
        calls = int(calls or 0)
        if step in ("video_bg", "image_bg"):
            ai_bg_by_user[user_id] = ai_bg_by_user.get(user_id, 0) + calls
        ai_cost_by_user[user_id] = (
            ai_cost_by_user.get(user_id, 0.0)
            + calls * cost_for_record(tool_name, tool_provider)
        )

    # --- 8. Last activity also considers audit actions (downloads, edits,
    #        logins) so "last seen" isn't blind to non-job actions. --------
    audit_last_rows = (
        db.query(AuditLog.user_id, func.max(AuditLog.created_at))
        .filter(AuditLog.created_at >= since, AuditLog.user_id.isnot(None))
        .group_by(AuditLog.user_id)
        .all()
    )
    for uid, last_audit in audit_last_rows:
        prev = last_activity_by_user.get(uid)
        if prev is None or (last_audit is not None and last_audit > prev):
            last_activity_by_user[uid] = last_audit

    # --- 9. Sesiones (tiempo en app + online ahora), solo con telemetría --
    # Agregado en Python y no en SQL: el volumen es mínimo (una row por
    # sesión, no por heartbeat) y así el cálculo de duraciones es idéntico
    # en SQLite (tests) y Postgres (prod), sin func.extract dialect-specific.
    sessions_by_user = {}
    telemetry_on = telemetry_enabled()
    if telemetry_on:
        now = datetime.now(timezone.utc)
        today_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        week_ago = now - timedelta(days=7)
        online_cutoff = now - timedelta(minutes=3)
        sess_rows = (
            db.query(UserSession)
            .filter(UserSession.last_seen_at >= week_ago)
            .all()
        )
        for s in sess_rows:
            # SQLite (tests) devuelve datetimes naive aunque la columna sea
            # timezone=True; Postgres devuelve aware. Mismo guard que reaper.py.
            started_at = s.started_at
            last_seen_at = s.last_seen_at
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=timezone.utc)
            if last_seen_at.tzinfo is None:
                last_seen_at = last_seen_at.replace(tzinfo=timezone.utc)
            agg = sessions_by_user.setdefault(s.user_id, {
                "online": False,
                "last_seen_at": None,
                "seconds_today": 0,
                "seconds_week": 0,
                "sessions_week": 0,
            })
            duration = max(0.0, (last_seen_at - started_at).total_seconds())
            agg["sessions_week"] += 1
            agg["seconds_week"] += int(duration)
            if last_seen_at >= today_start:
                # Clip de la sesión a la ventana de hoy para que una sesión
                # que arrancó ayer no cuente sus horas de ayer como de hoy.
                start_today = max(started_at, today_start)
                agg["seconds_today"] += int(max(0.0, (last_seen_at - start_today).total_seconds()))
            if last_seen_at >= online_cutoff:
                agg["online"] = True
            prev_seen = agg["last_seen_at"]
            seen_iso = last_seen_at.isoformat()
            if prev_seen is None or seen_iso > prev_seen:
                agg["last_seen_at"] = seen_iso
            # La sesión también cuenta como "última actividad" del usuario.
            prev_act = last_activity_by_user.get(s.user_id)
            if prev_act is not None and prev_act.tzinfo is None:
                prev_act = prev_act.replace(tzinfo=timezone.utc)
            if prev_act is None or last_seen_at > prev_act:
                last_activity_by_user[s.user_id] = last_seen_at

    # --- Stitch ------------------------------------------------------------
    users = db.query(User).all()
    empty_videos = {"total": 0, "done": 0, "approved": 0, "failed": 0, "in_progress": 0}
    empty_rework = {"variants": 0, "rerendered_jobs": 0, "total_edits": 0}
    empty_audit = {
        "edits_lyrics": 0, "edits_typography": 0, "edits_background": 0,
        "edits_metadata": 0, "retries": 0, "corrected_jobs": 0,
    }
    result = []
    for u in users:
        last_activity = last_activity_by_user.get(u.id)
        rework = dict(empty_rework, **rework_by_user.get(u.id, {}))
        rework.update(dict(empty_audit, **audit_rework_by_user.get(u.id, {})))
        rework["abandoned_recreated"] = recreated_by_user.get(u.id, 0)
        result.append({
            "user_id": u.id,
            "username": u.username,
            "email": u.email,
            "tenant_id": u.tenant_id,
            "plan": u.plan_id,
            "role": u.role,
            "is_active": u.is_active,
            "last_activity": last_activity.isoformat() if last_activity else None,
            "videos": videos_by_user.get(u.id, dict(empty_videos)),
            "errors": {
                "count": videos_by_user.get(u.id, empty_videos)["failed"],
                "recent": errors_by_user.get(u.id, []),
                "by_category": error_categories_by_user.get(u.id, {}),
            },
            "rework": rework,
            "backgrounds": {
                "library": library_by_user.get(u.id, 0),
                "ai_generated": ai_bg_by_user.get(u.id, 0),
            },
            "ai_cost_usd": round(ai_cost_by_user.get(u.id, 0.0), 2),
            # None cuando la telemetría está apagada o el usuario no tuvo
            # sesiones esta semana — el frontend lee con optional chaining.
            "sessions": sessions_by_user.get(u.id) if telemetry_on else None,
        })

    # Usuarios con actividad primero (más reciente arriba); los que nunca
    # hicieron nada en la ventana van al final.
    result.sort(key=lambda r: r["last_activity"] or "", reverse=True)

    return {
        "since_days": since_days,
        "since": since.isoformat(),
        "users": result,
        # Breakdown global de errores por categoría (para los chips del tab).
        "errors_by_category": errors_by_category_global,
        # Si está apagada, el frontend muestra "tracking deshabilitado" en
        # vez de columnas de tiempo en cero.
        "telemetry_enabled": telemetry_on,
    }


@router.get("/activity/{user_id}")
async def admin_activity_detail(
    user_id: int,
    admin: dict = Depends(require_super_admin),
    db: Session = Depends(get_db),
    since_days: int = Query(30, ge=1, le=365),
):
    """Drill-down for one user: job timeline, downloads and rework events."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    since = _activity_window(since_days)

    jobs = (
        db.query(Job)
        .filter(Job.user_id == user_id, Job.created_at >= since, ~Job.status.like("bg_preview%"))
        .order_by(Job.created_at.desc())
        .limit(100)
        .all()
    )
    # Resumen de la config elegida por job (render_params) + fuente del
    # fondo — backs la columna "elecciones" del perfil de usuario en
    # Insights. Aditivo: el shape histórico no cambia.
    from admin_insights import background_source_map, job_choices, user_detail_extras
    bg_sources = background_source_map(db, [j.job_id for j in jobs])
    job_rows = []
    for j in jobs:
        choices = job_choices(j)
        if choices is not None:
            choices["background_source"] = bg_sources.get(j.job_id, "other")
        job_rows.append({
            "job_id": j.job_id,
            "artist": j.artist,
            "song_title": j.song_title,
            "status": j.status,
            "current_step": j.current_step,
            "error": (j.error or "")[:300] if j.error else None,
            "timing_source": j.timing_source,
            "edit_count": j.edit_count or 0,
            "parent_job_id": j.parent_job_id,
            "approved_at": j.approved_at.isoformat() if j.approved_at else None,
            "created_at": j.created_at.isoformat() if j.created_at else None,
            "completed_at": j.completed_at.isoformat() if j.completed_at else None,
            "choices": choices,
        })

    audit_events = (
        db.query(AuditLog)
        .filter(
            AuditLog.user_id == user_id,
            AuditLog.created_at >= since,
            AuditLog.action.in_((
                _ACTIVITY_DOWNLOAD_ACTION,
                _ACTIVITY_EDIT_ACTION,
                _ACTIVITY_RETRY_ACTION,
                _ACTIVITY_SEGMENTS_ACTION,
                "job.variant_created",
                "job.approve",
                "job.reject",
            )),
        )
        .order_by(AuditLog.created_at.desc())
        .limit(200)
        .all()
    )
    downloads = []
    rework_events = []
    for e in audit_events:
        row = {
            "action": e.action,
            "detail": e.detail,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        if e.action == _ACTIVITY_DOWNLOAD_ACTION:
            downloads.append(row)
        else:
            rework_events.append(row)

    return {
        "user": user.to_dict(),
        "since_days": since_days,
        "jobs": job_rows,
        "downloads": downloads,
        "events": rework_events,
        # sessions (null sin TELEMETRY_ENABLED) / logins / library_usage —
        # consumidos por UserProfileView del panel Insights.
        **user_detail_extras(db, user_id, since),
    }


# ---------------------------------------------------------------------------
# Background Asset Library
# ---------------------------------------------------------------------------

@router.get("/backgrounds")
async def list_backgrounds(
    owner_tenant_id: Optional[str] = Query(None),
    admin: dict = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    """List all background assets.

    `owner_tenant_id` filters: pass a tenant string to see only that
    tenant's exclusive assets, or "__global__" to see only global ones
    (owner_tenant_id IS NULL). Omit to see everything.
    """
    q = db.query(BackgroundAsset)
    if owner_tenant_id == "__global__":
        q = q.filter(BackgroundAsset.owner_tenant_id.is_(None))
    elif owner_tenant_id:
        q = q.filter(BackgroundAsset.owner_tenant_id == owner_tenant_id)
    assets = q.order_by(BackgroundAsset.created_at.desc()).all()
    return [a.to_dict() for a in assets]


@router.get("/background-tenants")
async def list_background_tenants(
    admin: dict = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    """List the tenants that have at least one user, plus the special
    "global" entry. Used by the admin upload UI to populate the
    "Assign to tenant" dropdown without us hardcoding the UMG name."""
    tenants = [
        t[0]
        for t in db.query(User.tenant_id).distinct().order_by(User.tenant_id).all()
        if t[0]
    ]
    return {"tenants": tenants}


@router.post("/backgrounds")
async def upload_background(
    file: UploadFile = File(...),
    name: str = Form(...),
    tags: str = Form(""),
    owner_tenant_id: str = Form(""),
    admin: dict = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    """Upload a new pre-approved background asset.

    Storage strategy:
      - When R2 is configured (production), the file is streamed to a
        temp path then uploaded to R2 under `library/<uuid><ext>` and
        the local copy is removed. `BackgroundAsset.filename` stores
        the full R2 key so the read path can detect it via the
        `library/` prefix and serve via signed URL.
      - When R2 is disabled (local dev), falls back to disk write at
        BACKGROUNDS_DIR. Filename then is just the local basename.

    Either way the read path in main.py supports both shapes — the
    `library/` prefix is the signal.

    `owner_tenant_id` (optional form field): if provided, the asset is
    locked to that tenant — only users of that tenant (and admins) will
    see it in /backgrounds. Empty string means "global / visible to
    everyone", which is the right default for fallback assets but the
    wrong default for paying clients like UMG.
    """
    import storage

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in (".mp4", ".mov", ".jpg", ".jpeg", ".png"):
        raise HTTPException(status_code=400, detail="Only MP4, MOV, JPG, PNG files accepted.")

    file_type = "mp4" if ext in (".mp4", ".mov") else "jpg" if ext in (".jpg", ".jpeg") else "png"
    unique_basename = f"{uuid.uuid4().hex[:12]}{ext}"
    local_path = os.path.join(BACKGROUNDS_DIR, unique_basename)

    # Always write to disk first (R2 SDK uploads from a path, not a stream).
    with open(local_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    # Validate bytes before the object becomes visible in the catalogue.
    # The old endpoint trusted only the extension, which allowed the test
    # fixtures (zero-filled ``.mp4`` files) and malformed uploads to become
    # selectable production backgrounds.
    from main import _validate_background_file_on_disk
    _validate_background_file_on_disk(file.filename, local_path)
    try:
        probe = await asyncio.to_thread(
            subprocess.run,
            [
                "ffmpeg", "-nostdin", "-v", "error", "-xerror",
                "-i", local_path,
                "-map", "0:v:0",
                "-f", "null", "-",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        try:
            os.unlink(local_path)
        except OSError:
            pass
        logger.error("Background validation infrastructure failed: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="Background validation is temporarily unavailable.",
        ) from exc
    if probe.returncode != 0:
        try:
            os.unlink(local_path)
        except OSError:
            pass
        raise HTTPException(
            status_code=400,
            detail="File could not be decoded as supported background media.",
        )

    stored_filename = unique_basename
    storage_enabled = storage.is_enabled()
    if not storage_enabled and _requires_durable_background_storage():
        try:
            os.unlink(local_path)
        except OSError:
            pass
        raise HTTPException(
            status_code=503,
            detail="Background storage is not configured.",
        )
    if storage_enabled:
        r2_key = f"library/{unique_basename}"
        try:
            uploaded_key = storage.upload_file(local_path, r2_key)
            if uploaded_key != r2_key:
                raise RuntimeError("R2 upload did not confirm the object key")
            stored_filename = r2_key  # the `library/` prefix is the signal
            os.unlink(local_path)
        except Exception as e:
            logger.error(f"Failed to upload library asset to R2: {e}")
            # A local fallback is not durable across Railway replicas or
            # deploys. Fail closed and never create a DB row pointing at one
            # container's ephemeral filesystem.
            try:
                os.unlink(local_path)
            except OSError:
                pass
            raise HTTPException(
                status_code=503,
                detail="Background storage is temporarily unavailable.",
            ) from e

    tenant_scope = (owner_tenant_id or "").strip() or None
    asset = BackgroundAsset(
        name=name,
        filename=stored_filename,
        file_type=file_type,
        tags=tags.strip() if tags.strip() else None,
        uploaded_by=admin["id"],
        owner_tenant_id=tenant_scope,
    )
    db.add(asset)
    db.commit()
    db.refresh(asset)

    db.add(AuditLog(
        user_id=admin["id"],
        action="admin.upload_background",
        detail={
            "asset_id": asset.id,
            "name": name,
            "owner_tenant_id": tenant_scope,
            "storage": "r2" if stored_filename.startswith("library/") else "local",
        },
    ))
    db.commit()

    return asset.to_dict()


@router.delete("/backgrounds/{asset_id}")
async def delete_background(
    asset_id: int,
    admin: dict = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    """Delete a background asset (DB row + the underlying object)."""
    import storage

    asset = db.query(BackgroundAsset).filter(BackgroundAsset.id == asset_id).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")

    # Delete the underlying object — R2 if it lives there, local disk otherwise.
    if asset.filename.startswith("library/"):
        if not storage.is_enabled():
            raise HTTPException(
                status_code=503,
                detail="Background storage is not configured.",
            )
        try:
            client = storage._get_client()
            client.delete_object(Bucket=storage.R2_BUCKET, Key=asset.filename)
        except Exception as e:
            logger.error(f"Failed to delete R2 object {asset.filename}: {e}")
            raise HTTPException(
                status_code=503,
                detail="Background storage is temporarily unavailable.",
            ) from e
    else:
        file_path = os.path.join(BACKGROUNDS_DIR, asset.filename)
        if os.path.exists(file_path):
            os.unlink(file_path)

    db.delete(asset)
    db.add(AuditLog(
        user_id=admin["id"],
        action="admin.delete_background",
        detail={"asset_id": asset_id, "name": asset.name},
    ))
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Storage retention
# ---------------------------------------------------------------------------

@router.post("/cleanup-inputs")
async def cleanup_inputs(
    retention_days: int = Query(365, ge=1, le=3650),
    apply: bool = Query(False),
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Delete user-uploaded MP3 inputs in R2 once they pass the retention
    window. Inputs live under the `inputs/` prefix; deliverables and
    caches are not touched.

    HARDENED 2026-05-27 after the agus.cafisi incident (26 input audios
    lost to this exact endpoint between 5/11 and 5/18, no audit trail at
    the time). Three changes:
      1. `apply=true` requires the env var ALLOW_INPUT_CLEANUP=true.
         Default state is "the endpoint is disabled in delete mode" —
         accidental clicks now return 503 instead of nuking audios.
         If you ever need it, set the var in Railway dashboard, run
         the endpoint, then UNSET the var to relock.
      2. Default retention bumped 30 → 365 d. Storing 50 MB audios on
         R2 costs ~$0.0075/audio/year — keeping them indefinitely is
         orders of magnitude cheaper than recovering ONE lost UMG
         project. Max raised to 10 years for flexibility.
      3. Sentry capture_message fires on apply=true so the operator
         sees the action in observability even if they ignore the
         audit log.

    Default is still dry-run (apply=false) — gives the admin a preview
    of what WOULD be deleted before flipping the lock.
    """
    import os
    import storage

    if apply:
        if os.environ.get("ALLOW_INPUT_CLEANUP", "").strip().lower() != "true":
            raise HTTPException(
                status_code=503,
                detail=(
                    "Input cleanup is disabled by default. To enable: set "
                    "env var ALLOW_INPUT_CLEANUP=true in Railway dashboard, "
                    "deploy, run this endpoint, then UNSET the var immediately "
                    "to relock. See the 2026-05-27 agus.cafisi incident."
                ),
            )
        # Sentry breadcrumb for delete-mode invocations. Even with the
        # env-var guard, we want this action to scream in observability
        # so a future repeat of the incident is visible within seconds.
        try:
            import sentry_sdk
            sentry_sdk.capture_message(
                f"[ADMIN-CLEANUP-INPUTS] user_id={admin['id']} "
                f"retention_days={retention_days} prefix=inputs/",
                level="warning",
            )
        except Exception:
            pass

    report = storage.cleanup_old_inputs(
        retention_days=retention_days,
        apply=apply,
        prefix="inputs/",
    )
    db.add(AuditLog(
        user_id=admin["id"],
        action="admin.cleanup_inputs.apply" if apply else "admin.cleanup_inputs.dryrun",
        detail={
            "retention_days": retention_days,
            "scanned": report.get("scanned"),
            "expired": report.get("expired"),
            "deleted": report.get("deleted"),
        },
    ))
    db.commit()
    return report


# ---------------------------------------------------------------------------
# Métricas de decisión — Fases 1+2 del panel world-class (2026-06-11).
# Lógica en admin_metrics.py; acá solo routing + gates.
# ---------------------------------------------------------------------------

@router.get("/metrics/timeseries")
async def admin_metrics_timeseries(
    days: int = Query(28, ge=7, le=90),
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Series por día/tenant (creados, aprobados, retrabajos, costo IA).
    El frontend deriva los deltas WoW de las dos ventanas de 7 días."""
    from admin_metrics import metrics_timeseries
    return metrics_timeseries(db, days=days)


@router.get("/metrics/funnel")
async def admin_metrics_funnel(
    days: int = Query(7, ge=1, le=28),
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Funnel operativo con conversión y p50/p95 por etapa."""
    from admin_metrics import metrics_funnel
    return metrics_funnel(db, days=days)


@router.get("/metrics/economics")
async def admin_metrics_economics(
    days: int = Query(28, ge=7, le=90),
    admin: dict = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    """Margen por tenant (revenue del plan vs costo IA real). Información
    de negocio sensible → gate super-admin (SUPER_ADMIN_USERS)."""
    from admin_metrics import metrics_economics
    return metrics_economics(db, days=days)


@router.get("/metrics/health")
async def admin_metrics_health(
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Health score 0-100 por tenant con componentes (uso WoW, first-pass,
    retrabajo, errores). Los umbrales de alerta viven en admin_metrics."""
    from admin_metrics import metrics_health
    return metrics_health(db)


# ---------------------------------------------------------------------------
# Insights de comportamiento — panel CEO (2026-06-10). Lógica en
# admin_insights.py; acá solo routing + gates. TODO super-admin: es
# observabilidad de usuarios, no del tenant.
# ---------------------------------------------------------------------------

@router.get("/insights/adoption")
async def admin_insights_adoption(
    days: int = Query(30, ge=1, le=365),
    tenant_id: Optional[str] = Query(None),
    user_id: Optional[int] = Query(None),
    admin: dict = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    """Qué features usa la gente (agregación de Job.render_params): animaciones,
    transiciones, efectos, tipografías, estilos, fuente del fondo. Un solo
    endpoint para los 3 niveles vía tenant_id/user_id."""
    from admin_insights import insights_adoption
    return insights_adoption(db, days=days, tenant_id=tenant_id, user_id=user_id)


@router.get("/insights/overview")
async def admin_insights_overview(
    days: int = Query(30, ge=1, le=365),
    tenant_id: Optional[str] = Query(None),
    admin: dict = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    """KPIs de uso/calidad/costo + ranking de tenants y usuarios para el
    drill-down del panel Insights."""
    from admin_insights import insights_overview
    return insights_overview(db, days=days, tenant_id=tenant_id)


@router.get("/insights/wizard")
async def admin_insights_wizard(
    days: int = Query(30, ge=1, le=365),
    tenant_id: Optional[str] = Query(None),
    user_id: Optional[int] = Query(None),
    admin: dict = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    """Funnel del wizard desde ui_events (pasos alcanzados, abandono,
    conversión a generate). {empty: true} hasta que la telemetría acumule."""
    from admin_insights import insights_wizard
    return insights_wizard(db, days=days, tenant_id=tenant_id, user_id=user_id)


@router.get("/insights/feature/{feature}")
async def admin_insights_feature_detail(
    feature: str,
    value: str = Query(..., max_length=120),
    days: int = Query(30, ge=1, le=365),
    tenant_id: Optional[str] = Query(None),
    admin: dict = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    """Drill de una barra de adopción: qué usuarios y videos usan ese
    valor de la feature (whitelist en admin_insights)."""
    from admin_insights import insights_feature_detail
    return insights_feature_detail(db, feature, value, days=days, tenant_id=tenant_id)


@router.get("/insights/job/{job_id}")
async def admin_insights_job_detail(
    job_id: str,
    admin: dict = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    """Ficha completa de un video: parámetros, llamadas IA con costo,
    eventos y error."""
    from admin_insights import insights_job_detail
    detail = insights_job_detail(db, job_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return detail


@router.get("/insights/user/{user_id}/events")
async def admin_insights_user_events(
    user_id: int,
    days: int = Query(30, ge=1, le=365),
    admin: dict = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    """Sesiones de wizard del usuario, reconstruidas desde ui_events."""
    from admin_insights import insights_user_events
    return insights_user_events(db, user_id, days=days)
