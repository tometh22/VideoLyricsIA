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

from auth import get_current_user, PLANS, pwd_context, validate_password_strength
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
    # tenant_id: when omitted, auth.create_user() auto-generates it from
    # the username — fine for solo accounts but produces an isolated
    # tenant per user. Pass it explicitly to put several teammates into
    # the same shared workspace (e.g. all UMG operators on
    # tenant_id="universal_music" so they see each other's jobs).
    tenant_id: str = ""
    # If true, the user keeps generating past plan monthly limit and we
    # invoice the overage out-of-band. Default False = hard wall at limit.
    allow_overage: bool = False


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
            tenant_id=body.tenant_id.strip() or None,
        )
        if body.allow_overage:
            user.allow_overage = True
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
    pipeline live."""
    query = db.query(Job).order_by(Job.created_at.desc())
    if status:
        query = query.filter(Job.status == status)
    if tenant_id:
        query = query.filter(Job.tenant_id == tenant_id)

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
    retention_days: int = Query(30, ge=1, le=365),
    apply: bool = Query(False),
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Delete user-uploaded MP3 inputs in R2 once they pass the retention
    window. Inputs live under the `inputs/` prefix; deliverables and
    caches are not touched.

    Default is dry-run (apply=false) so the admin sees what would be
    deleted before doing it. Pass apply=true to actually delete.
    """
    import storage
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
