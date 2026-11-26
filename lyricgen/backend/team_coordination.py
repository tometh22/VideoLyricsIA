"""Assignment is coordination metadata, never an override of production/review state."""

from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import case, func, or_
from sqlalchemy.orm import Session

from auth import get_current_user
from batch_campaigns import _require_scope, _now, _phase
from database import (
    BatchCampaign,
    BatchCampaignItem,
    EditorDocument,
    Job,
    TeamActivity,
    TeamAssignment,
    TeamCoordinator,
    User,
    get_db,
)

router = APIRouter(prefix="/team", tags=["team"])


def _tenant(user, requested=None):
    tenant = requested or user["tenant_id"]
    if tenant != user["tenant_id"] and user.get("role") != "admin":
        raise HTTPException(403, "No tenés acceso a este equipo")
    _require_scope({**user, "tenant_id": tenant})
    return tenant


def _manages(db, user, tenant):
    return (
        user.get("role") == "admin"
        or db.get(TeamCoordinator, (tenant, user["id"])) is not None
    )


def _require_coordinator(db, user, tenant):
    if not _manages(db, user, tenant):
        raise HTTPException(403, "Se requiere permiso de coordinación")


def _member(db, user_id, tenant):
    member = (
        db.query(User)
        .filter(User.id == user_id, User.tenant_id == tenant, User.is_active.is_(True))
        .with_for_update()
        .first()
    )
    if not member:
        raise HTTPException(
            422, "El responsable debe ser un usuario activo de este equipo"
        )
    return member


def _event(db, user, tenant, action, detail, item_id=None):
    db.add(
        TeamActivity(
            tenant_id=tenant,
            actor_id=user["id"],
            action=action,
            detail=detail,
            item_id=item_id,
        )
    )


def _state():
    # Final acceptance wins over stale coordination notes. Machine processing is
    # distinct from human work, and a lyrics approval is NOT a finished video.
    return case(
        (Job.status == "done", "done"),
        (Job.status == "discarded", "discarded"),
        (func.coalesce(TeamAssignment.blocked_reason, "") != "", "blocked"),
        (
            Job.status.in_(
                ("error", "transcription_failed", "validation_failed", "rejected")
            ),
            "failed",
        ),
        (BatchCampaignItem.upload_state == "error", "failed"),
        (Job.status == "pending_review", "review"),
        (
            Job.status.in_(
                (
                    "awaiting_upload",
                    "transcribing_queued",
                    "transcribing",
                    "separation_queued",
                    "separating",
                    "lyrics_approved",
                    "queued",
                    "processing",
                    "editing",
                    "background_generating",
                    "rendering",
                )
            ),
            "processing",
        ),
        (
            or_(
                TeamAssignment.started_at.isnot(None),
                EditorDocument.lock_expires_at > _now(),
            ),
            "working",
        ),
        else_="pending",
    )


def _base(db, tenant):
    return (
        db.query(BatchCampaignItem.id)
        .join(
            BatchCampaign,
            BatchCampaign.id == BatchCampaignItem.campaign_id,
        )
        .outerjoin(Job, Job.campaign_item_id == BatchCampaignItem.id)
        .outerjoin(
            TeamAssignment,
            TeamAssignment.item_id == BatchCampaignItem.id,
        )
        .outerjoin(EditorDocument, EditorDocument.job_id == Job.job_id)
        .filter(
            BatchCampaignItem.tenant_id == tenant,
            BatchCampaign.tenant_id == tenant,
            BatchCampaign.status != "cancelled",
        )
    )


def _counts(query):
    state = _state()
    return dict(
        query.with_entities(state, func.count(BatchCampaignItem.id))
        .group_by(state)
        .all()
    )


@router.get("/access")
def access(
    current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)
):
    tenant = _tenant(current_user)
    tenants = [tenant]
    if current_user.get("role") == "admin":
        tenants = [
            t
            for (t,) in db.query(User.tenant_id)
            .distinct()
            .order_by(User.tenant_id)
            .all()
        ]
    return {
        "can_manage": _manages(db, current_user, tenant),
        "can_grant": current_user.get("role") == "admin",
        "tenant_id": tenant,
        "tenants": tenants,
    }


@router.get("/dashboard")
def dashboard(
    tenant_id: str | None = None,
    mine: bool = False,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant = _tenant(current_user, tenant_id)
    manages = _manages(db, current_user, tenant)
    if not manages:
        mine = True
    query = _base(db, tenant)
    if mine:
        query = query.filter(TeamAssignment.assignee_id == current_user["id"])
    counts = _counts(query)
    state = _state()
    by_person = (
        query.with_entities(TeamAssignment.assignee_id, state, func.count())
        .group_by(TeamAssignment.assignee_id, state)
        .all()
    )
    by_campaign = (
        query.with_entities(BatchCampaign.id, BatchCampaign.name, state, func.count())
        .group_by(BatchCampaign.id, BatchCampaign.name, state)
        .all()
    )
    campaigns = {}
    for cid, name, state, total in by_campaign:
        campaigns.setdefault(cid, {"id": cid, "name": name, "counts": {}})["counts"][
            state
        ] = total
    members_query = db.query(User).filter(User.tenant_id == tenant)
    if mine:
        members_query = members_query.filter(User.id == current_user["id"])
    grants = {
        uid
        for (uid,) in db.query(TeamCoordinator.user_id)
        .filter(TeamCoordinator.tenant_id == tenant)
        .all()
    }
    members = [
        {
            "id": u.id,
            "name": u.full_name or u.username,
            "active": u.is_active,
            "coordinator": u.id in grants,
            "counts": {s: n for uid, s, n in by_person if uid == u.id},
        }
        for u in members_query.order_by(User.username).all()
    ]
    activity_query = (
        db.query(TeamActivity, User.username, User.full_name)
        .join(User, User.id == TeamActivity.actor_id)
        .filter(TeamActivity.tenant_id == tenant)
    )
    if mine:
        activity_query = activity_query.filter(
            TeamActivity.item_id.in_(query.with_entities(BatchCampaignItem.id))
        )
    events = [
        {
            "id": e.id,
            "action": e.action,
            "detail": e.detail,
            "actor": full or username,
            "created_at": e.created_at.isoformat(),
        }
        for e, username, full in activity_query.order_by(TeamActivity.id.desc())
        .limit(20)
        .all()
    ]
    return {
        "can_manage": manages,
        "counts": counts,
        "members": members,
        "campaigns": list(campaigns.values()),
        "activity": events,
        "unassigned": sum(
            n
            for uid, state, n in by_person
            if uid is None and state not in ("done", "discarded")
        ),
    }


@router.get("/tasks")
def tasks(
    tenant_id: str | None = None,
    mine: bool = False,
    campaign_id: str | None = None,
    assignee_id: int | None = None,
    unassigned: bool = False,
    state: str | None = None,
    search: str = Query(default="", max_length=200),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=1000),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant = _tenant(current_user, tenant_id)
    query = _base(db, tenant)
    if mine or not _manages(db, current_user, tenant):
        query = query.filter(TeamAssignment.assignee_id == current_user["id"])
    if campaign_id:
        query = query.filter(BatchCampaignItem.campaign_id == campaign_id)
    if assignee_id is not None:
        query = query.filter(TeamAssignment.assignee_id == assignee_id)
    if unassigned:
        query = query.filter(
            TeamAssignment.assignee_id.is_(None), _state().notin_(("done", "discarded"))
        )
    if state:
        query = query.filter(_state() == state)
    if search.strip():
        query = query.filter(
            or_(
                BatchCampaignItem.title.icontains(search.strip(), autoescape=True),
                BatchCampaignItem.artist.icontains(search.strip(), autoescape=True),
                BatchCampaignItem.filename.icontains(search.strip(), autoescape=True),
            )
        )
    total = query.count()
    rows = (
        query.with_entities(
            BatchCampaignItem.id,
            BatchCampaignItem.title,
            BatchCampaignItem.filename,
            BatchCampaignItem.artist,
            BatchCampaign.id,
            BatchCampaign.name,
            BatchCampaignItem.upload_state,
            BatchCampaignItem.metadata_error,
            Job.job_id,
            Job.status,
            _state(),
            TeamAssignment,
            EditorDocument.lock_user_id,
            EditorDocument.lock_expires_at,
        )
        .order_by(
            BatchCampaignItem.campaign_id,
            BatchCampaignItem.ordinal,
            BatchCampaignItem.id,
        )
        .offset(offset)
        .limit(limit)
        .all()
    )
    result = []
    for (
        iid,
        title,
        filename,
        artist,
        cid,
        name,
        upload,
        error,
        jid,
        job_status,
        status,
        a,
        lock_user,
        lock_expiry,
    ) in rows:
        result.append(
            {
                "id": iid,
                "title": title or filename,
                "artist": artist,
                "campaign_id": cid,
                "campaign": name,
                "job_id": jid,
                "status": status,
                "phase": _phase(upload, job_status, error),
                "assignee_id": a.assignee_id if a else None,
                "priority": a.priority if a else "normal",
                "due_date": a.due_date.isoformat() if a and a.due_date else None,
                "note": a.note if a else "",
                "blocked_reason": a.blocked_reason if a else "",
                "version": a.version if a else 0,
                "updated_at": a.updated_at.isoformat() if a else None,
                "lock_user_id": lock_user,
                "lock_expires_at": lock_expiry.isoformat() if lock_expiry else None,
            }
        )
    return {"items": result, "total": total, "offset": offset, "limit": limit}


class AssignmentItem(BaseModel):
    id: str = Field(min_length=1, max_length=36)
    version: int = Field(ge=0)


class AssignRequest(BaseModel):
    items: list[AssignmentItem] = Field(min_length=1, max_length=1000)
    assignee_id: int | None
    priority: Literal["normal", "high"] = "normal"
    due_date: date | None = None


@router.post("/assignments")
def assign(
    body: AssignRequest,
    tenant_id: str | None = None,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant = _tenant(current_user, tenant_id)
    _require_coordinator(db, current_user, tenant)
    if body.assignee_id is not None:
        _member(db, body.assignee_id, tenant)
    versions = {i.id: i.version for i in body.items}
    if len(versions) != len(body.items):
        raise HTTPException(422, "La selección contiene videos repetidos")
    # Lock the durable parent even when an assignment row does not exist yet.
    # Sorted locks serialize overlapping bulk operations without deadlocks.
    items = (
        db.query(BatchCampaignItem)
        .filter(
            BatchCampaignItem.id.in_(versions), BatchCampaignItem.tenant_id == tenant
        )
        .order_by(BatchCampaignItem.id)
        .with_for_update()
        .all()
    )
    if len(items) != len(versions):
        raise HTTPException(404, "Uno o más videos no pertenecen al equipo")
    changed, skipped = [], []
    try:
        for item in items:
            campaign = db.get(BatchCampaign, item.campaign_id)
            job = db.query(Job).filter(Job.campaign_item_id == item.id).first()
            if campaign.status in ("cancelled", "completed") or (
                job and job.status in ("done", "discarded")
            ):
                skipped.append(item.id)
                continue
            a = (
                db.query(TeamAssignment)
                .filter_by(item_id=item.id)
                .populate_existing()
                .first()
            )
            if (a.version if a else 0) != versions[item.id]:
                raise HTTPException(
                    409, "La asignación cambió. Actualizá la lista y volvé a intentarlo"
                )
            if a is None:
                a = TeamAssignment(
                    item_id=item.id,
                    tenant_id=tenant,
                    version=0,
                    note="",
                    blocked_reason="",
                )
                db.add(a)
            previous = a.assignee_id
            a.assignee_id, a.priority, a.due_date = (
                body.assignee_id,
                body.priority,
                body.due_date,
            )
            a.version += 1
            a.updated_by, a.updated_at = current_user["id"], _now()
            _event(
                db,
                current_user,
                tenant,
                "assigned",
                {
                    "title": item.title or item.filename,
                    "from": previous,
                    "to": body.assignee_id,
                    "priority": body.priority,
                    "due_date": str(body.due_date) if body.due_date else None,
                },
                item.id,
            )
            changed.append(item.id)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"assigned": changed, "skipped": skipped}


class WorkRequest(BaseModel):
    version: int = Field(ge=1)
    action: Literal["start", "note", "block", "unblock"]
    note: str = Field(default="", max_length=2000)
    blocked_reason: str = Field(default="", max_length=1000)


@router.patch("/tasks/{item_id}")
def update_work(
    item_id: str,
    body: WorkRequest,
    tenant_id: str | None = None,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant = _tenant(current_user, tenant_id)
    item = (
        db.query(BatchCampaignItem)
        .filter_by(id=item_id, tenant_id=tenant)
        .with_for_update()
        .first()
    )
    if not item:
        raise HTTPException(404, "Video no encontrado")
    a = db.query(TeamAssignment).filter_by(item_id=item_id).populate_existing().first()
    if not a or (
        a.assignee_id != current_user["id"] and not _manages(db, current_user, tenant)
    ):
        raise HTTPException(
            403, "Solo el responsable o la coordinación pueden actualizar esta tarea"
        )
    if a.version != body.version:
        raise HTTPException(409, "La tarea cambió. Actualizá la lista antes de guardar")
    job = db.query(Job).filter_by(campaign_item_id=item_id).first()
    if db.get(BatchCampaign, item.campaign_id).status in ("completed", "cancelled") or (
        job and job.status in ("done", "discarded")
    ):
        raise HTTPException(409, "La tarea ya está aprobada o descartada")
    if body.action == "block" and not body.blocked_reason.strip():
        raise HTTPException(422, "Indicá el motivo del bloqueo")
    if body.action == "start":
        a.started_at = a.started_at or _now()
    if body.action == "block":
        a.blocked_reason = body.blocked_reason.strip()
    if body.action == "unblock":
        a.blocked_reason = ""
    a.note = body.note.strip()
    a.version += 1
    a.updated_at, a.updated_by = _now(), current_user["id"]
    _event(
        db,
        current_user,
        tenant,
        body.action,
        {"title": item.title or item.filename},
        item_id,
    )
    db.commit()
    return {"version": a.version}


class CoordinatorRequest(BaseModel):
    enabled: bool


@router.put("/coordinators/{user_id}")
def grant_coordinator(
    user_id: int,
    body: CoordinatorRequest,
    tenant_id: str | None = None,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant = _tenant(current_user, tenant_id)
    if current_user.get("role") != "admin":
        raise HTTPException(
            403, "Solo un administrador puede habilitar la coordinación"
        )
    # Lock member to serialize concurrent grants/revocations, including inserts.
    member = (
        db.query(User).filter_by(id=user_id, tenant_id=tenant).with_for_update().first()
    )
    if not member or (body.enabled and not member.is_active):
        raise HTTPException(422, "Usuario no disponible en este equipo")
    grant = db.get(TeamCoordinator, (tenant, user_id))
    if body.enabled and not grant:
        db.add(
            TeamCoordinator(
                tenant_id=tenant, user_id=user_id, granted_by=current_user["id"]
            )
        )
    elif not body.enabled and grant:
        db.delete(grant)
    _event(
        db,
        current_user,
        tenant,
        "coordinator",
        {"user_id": user_id, "enabled": body.enabled},
    )
    db.commit()
    return {"enabled": body.enabled}
