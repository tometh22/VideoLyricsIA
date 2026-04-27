"""Admin panel API for GenLy AI."""

import logging
import os
import shutil
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, File, UploadFile, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from auth import get_current_user, PLANS, pwd_context
from database import User, Job, Invoice, AuditLog, AIProvenance, BackgroundAsset, get_db

BACKGROUNDS_DIR = os.path.join(os.path.dirname(__file__), "..", "assets", "backgrounds", "library")
os.makedirs(BACKGROUNDS_DIR, exist_ok=True)

logger = logging.getLogger("genly.admin")

router = APIRouter(prefix="/admin", tags=["admin"])


def require_admin(current_user: dict = Depends(get_current_user)):
    """Dependency that ensures the user is an admin."""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
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


@router.post("/users")
async def create_user_admin(
    body: CreateUserRequest,
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Create a new user (admin)."""
    from auth import create_user
    try:
        user = create_user(
            db,
            username=body.username,
            password=body.password,
            email=body.email or None,
            role=body.role,
            plan=body.plan_id,
        )
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


@router.patch("/users/{user_id}")
async def update_user_admin(
    user_id: int,
    body: UpdateUserRequest,
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Update a user's plan, role, or status."""
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
        user.hashed_password = pwd_context.hash(body.password)
    if body.max_videos_per_day is not None:
        # Allow 0 to mean "block all uploads"; clamp to non-negative.
        user.max_videos_per_day = max(0, int(body.max_videos_per_day))
    if body.max_concurrent_jobs is not None:
        # Min 1 — a cap of 0 would block uploads entirely; use is_active=False
        # for that. Clamp negatives to 1.
        user.max_concurrent_jobs = max(1, int(body.max_concurrent_jobs))

    db.commit()
    db.refresh(user)

    # Audit
    db.add(AuditLog(
        user_id=admin["id"],
        action="admin.update_user",
        detail={"target_user": user_id, "changes": body.model_dump(exclude_none=True)},
    ))
    db.commit()

    return user.to_dict()


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

@router.get("/jobs")
async def list_all_jobs(
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
    limit: int = Query(50, le=500),
    offset: int = Query(0, ge=0),
    status: str = Query(""),
):
    """List all jobs across all tenants."""
    query = db.query(Job).order_by(Job.created_at.desc())
    if status:
        query = query.filter(Job.status == status)

    total = query.count()
    jobs = query.offset(offset).limit(limit).all()

    return {
        "total": total,
        "jobs": [j.to_dict() for j in jobs],
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
# Background Asset Library
# ---------------------------------------------------------------------------

@router.get("/backgrounds")
async def list_backgrounds(
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """List all background assets."""
    assets = db.query(BackgroundAsset).order_by(BackgroundAsset.created_at.desc()).all()
    return [a.to_dict() for a in assets]


@router.post("/backgrounds")
async def upload_background(
    file: UploadFile = File(...),
    name: str = Form(...),
    tags: str = Form(""),
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Upload a new pre-approved background asset."""
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in (".mp4", ".mov", ".jpg", ".jpeg", ".png"):
        raise HTTPException(status_code=400, detail="Only MP4, MOV, JPG, PNG files accepted.")

    file_type = "mp4" if ext in (".mp4", ".mov") else "jpg" if ext in (".jpg", ".jpeg") else "png"
    unique_name = f"{uuid.uuid4().hex[:12]}{ext}"
    file_path = os.path.join(BACKGROUNDS_DIR, unique_name)

    with open(file_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    asset = BackgroundAsset(
        name=name,
        filename=unique_name,
        file_type=file_type,
        tags=tags.strip() if tags.strip() else None,
        uploaded_by=admin["id"],
    )
    db.add(asset)
    db.commit()
    db.refresh(asset)

    db.add(AuditLog(
        user_id=admin["id"],
        action="admin.upload_background",
        detail={"asset_id": asset.id, "name": name},
    ))
    db.commit()

    return asset.to_dict()


@router.delete("/backgrounds/{asset_id}")
async def delete_background(
    asset_id: int,
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Delete a background asset."""
    asset = db.query(BackgroundAsset).filter(BackgroundAsset.id == asset_id).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")

    # Delete file
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
