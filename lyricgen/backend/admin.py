"""Admin panel API for GenLy AI."""

import logging
import os
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, File, UploadFile, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from auth import (
    get_current_user,
    PLANS,
    pwd_context,
    telemetry_enabled,
    validate_password_strength,
    _super_admin_allowlist,
)
from database import User, Job, Invoice, AuditLog, AIProvenance, AssetUsage, BackgroundAsset, UserSession, get_db
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
    identificados por username o email — pasan. Sin la var (dev/tests/
    staging) alcanza con role=admin, igual que require_admin.
    """
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    allow = _super_admin_allowlist()
    if allow:
        idents = {
            (current_user.get("username") or "").lower(),
            (current_user.get("email") or "").lower(),
        }
        if not (idents & allow):
            raise HTTPException(status_code=403, detail="Super admin access required")
    return current_user


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
    admin: dict = Depends(require_admin),
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
    admin: dict = Depends(require_admin),
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
    admin: dict = Depends(require_admin),
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

    stored_filename = unique_basename
    if storage.is_enabled():
        r2_key = f"library/{unique_basename}"
        try:
            storage.upload_file(local_path, r2_key)
            stored_filename = r2_key  # the `library/` prefix is the signal
            os.unlink(local_path)
        except Exception as e:
            logger.error(f"Failed to upload library asset to R2: {e}")
            # Keep the local copy as a fallback. Filename stays as the
            # bare basename so the read path uses the disk branch.

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
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Delete a background asset (DB row + the underlying object)."""
    import storage

    asset = db.query(BackgroundAsset).filter(BackgroundAsset.id == asset_id).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")

    # Delete the underlying object — R2 if it lives there, local disk otherwise.
    if asset.filename.startswith("library/") and storage.is_enabled():
        try:
            client = storage._get_client()
            client.delete_object(Bucket=storage.R2_BUCKET, Key=asset.filename)
        except Exception as e:
            logger.warning(f"Failed to delete R2 object {asset.filename}: {e}")
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
