"""Read-only inbox of client change requests for ONE campaign.

Client requests live in the shared portal database and, until now, could only
be seen in Admin > Cambios UMG. This lets the people who run a campaign see the
requests that belong to it, with the state of each, without granting any new
power: every action still happens in the existing correction workflow.

Scope is the campaign's own lineage (its songs and their variants) AND its
tenant, so a request for another tenant's song with a colliding job id can
never appear. The portal database is read once, briefly, and released before
any local work continues. ``owner_email`` is never returned.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

import delivery_freshness
from auth import get_current_user
from change_request_workflow import latest_overwrite, render_state, timestamp
from batch_campaigns import _aware, _campaign_or_404, _require_scope
from campaign_pipeline import _load_items, _load_lineage
from batch_campaigns import _require_manager
from database import (
    AuditLog, Delivery, EditorDocument, DeliveryChangeRequest, Job, deliveries_added_by, get_db, get_deliveries_db, scoped_deliveries_db,
)

router = APIRouter(prefix="/batch/campaigns", tags=["campaign-change-requests"])

_RENDERING = frozenset({"queued", "processing", "rendering", "editing", "background_generating", "transcribed_pending"})
_FINISHED = frozenset({"done", "pending_review"})
MAX_PAGE = 200


def feature_enabled() -> bool:
    return os.environ.get("CAMPAIGN_CHANGE_REQUESTS_ENABLED", "0") == "1"


def actions_enabled() -> bool:
    return os.environ.get("CAMPAIGN_CHANGE_REQUEST_ACTIONS", "0") == "1"


def _iso(value: datetime | None) -> str | None:
    value = _aware(value)
    return value.isoformat() if value else None


def closable_on_publish(request, job, delivery, document=None) -> tuple[bool, str]:
    """Whether publishing ``job``'s current cut may close ``request``.

    One rule shared by the inbox (what to offer), the send endpoint (what to
    accept) and the worker (what to apply), so they cannot disagree. Evidence
    only: the request is still open, was submitted BEFORE the corrected render
    finished, that render matches what the editor saved, and the portal still
    shows an older cut.
    """
    if request.resolved_at is not None:
        return False, "not_open"
    if job is None or job.status not in _FINISHED:
        return False, "job_not_ready"
    if not delivery_freshness.needs_publish(job, delivery):
        return False, "content_unchanged"
    rendered_at = latest_overwrite(job) or timestamp(job.completed_at)
    submitted_at = timestamp(request.submitted_at)
    if rendered_at is None or submitted_at is None or submitted_at > rendered_at:
        return False, "newer_than_cut"
    if document is not None and not render_state(job, document, request)["render_matches_editor"]:
        return False, "render_not_current"
    return True, "ok"


def _step(request: DeliveryChangeRequest, job: Job | None, delivery: Delivery) -> dict[str, str]:
    """Where an OPEN or closed request stands, from persisted evidence only."""
    if request.resolved_at:
        if request.resolution_source == "publication":
            revision = request.resolved_by_revision or delivery.published_revision
            return {"key": "resolved", "tone": "done", "label": f"Resuelto al publicar la v{revision}" if revision else "Resuelto al publicar"}
        return {"key": "closed", "tone": "idle", "label": "Cerrado sin publicar otro video"}
    if job is None:
        return {"key": "unknown", "tone": "attention", "label": "Estado por verificar"}
    if job.status in _RENDERING:
        return {"key": "rendering", "tone": "busy", "label": "Generando corte nuevo"}
    if job.status == "error":
        return {"key": "blocked", "tone": "attention", "label": "La generación falló"}
    if job.status in _FINISHED and delivery_freshness.needs_publish(job, delivery):
        return {"key": "publish", "tone": "attention", "label": "Corregido: falta publicar"}
    return {"key": "correct", "tone": "idle", "label": "Sin atender"}


def _client_approval(delivery: Delivery) -> str | None:
    """The client's own verdict on the published cut, independent of ours."""
    if delivery.approved_at is not None:
        return "approved"
    if delivery.content_updated_at is not None:
        return "pending"
    return None


def _parse_cursor(cursor: str | None) -> tuple[datetime, int] | None:
    if not cursor:
        return None
    try:
        stamp, raw_id = cursor.rsplit("|", 1)
        return datetime.fromisoformat(stamp), int(raw_id)
    except ValueError:
        raise HTTPException(status_code=400, detail={"code": "invalid_cursor"})


@router.get("/{campaign_id}/change-requests")
def campaign_change_requests(
    campaign_id: str,
    status: str = Query("open", pattern="^(open|resolved|all)$"),
    limit: int = Query(50, ge=1, le=MAX_PAGE),
    cursor: str | None = None,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_scope(current_user)
    campaign = _campaign_or_404(db, campaign_id, current_user)
    if not feature_enabled():
        raise HTTPException(status_code=404, detail={"code": "feature_disabled"})
    after = _parse_cursor(cursor)

    items = _load_items(db, [campaign.id])[campaign.id]
    jobs = _load_lineage(db, [campaign], full=True)[campaign.id]
    jobs_by_id = {job.job_id: job for job in jobs}
    item_by_id = {item.id: item for item in items}

    def song_of(job: Job | None):
        seen: set[str] = set()
        while job is not None and job.job_id not in seen:
            seen.add(job.job_id)
            if job.campaign_item_id in item_by_id:
                return item_by_id[job.campaign_item_id]
            job = jobs_by_id.get(job.parent_job_id)
        return None

    # A song's CURRENT job is its newest live cut: a request on an older
    # delivery is answered by what the song would publish next.
    current_by_song: dict[str, Job] = {}
    for job in sorted(jobs, key=lambda j: (_aware(j.created_at) or datetime.min.replace(tzinfo=timezone.utc), j.job_id)):
        song = song_of(job)
        if song is not None and job.status not in ("error", "failed", "discarded", "rejected"):
            current_by_song[song.id] = job

    empty = {"campaign_id": campaign.id, "available": True, "counts": {"open": 0, "resolved": 0, "oldest_open_at": None},
             "items": [], "next_cursor": None}
    if not jobs_by_id:
        return empty
    order_key = func.coalesce(DeliveryChangeRequest.updated_at, DeliveryChangeRequest.submitted_at)
    try:
        with scoped_deliveries_db() as ddb:
            scope = and_(
                Delivery.job_id.in_(list(jobs_by_id)), Delivery.tenant_snapshot == campaign.tenant_id,
                Delivery.removed_at.is_(None),
            )
            counted = ddb.query(
                func.count(DeliveryChangeRequest.id).filter(DeliveryChangeRequest.resolved_at.is_(None)),
                func.count(DeliveryChangeRequest.id).filter(DeliveryChangeRequest.resolved_at.isnot(None)),
                func.min(DeliveryChangeRequest.submitted_at).filter(DeliveryChangeRequest.resolved_at.is_(None)),
            ).join(Delivery, Delivery.id == DeliveryChangeRequest.delivery_id).filter(scope).one()
            query = ddb.query(DeliveryChangeRequest, Delivery).join(
                Delivery, Delivery.id == DeliveryChangeRequest.delivery_id,
            ).filter(scope)
            if status == "open":
                query = query.filter(DeliveryChangeRequest.resolved_at.is_(None))
            elif status == "resolved":
                query = query.filter(DeliveryChangeRequest.resolved_at.isnot(None))
            if after is not None:
                stamp = after[0] if after[0].tzinfo else after[0].replace(tzinfo=timezone.utc)
                query = query.filter(or_(order_key < stamp, and_(order_key == stamp, DeliveryChangeRequest.id < after[1])))
            rows = query.order_by(order_key.desc(), DeliveryChangeRequest.id.desc()).limit(limit + 1).all()
            page, more = rows[:limit], len(rows) > limit
            current_ids = set()
            for _request, _delivery in page:
                _song = song_of(jobs_by_id.get(_delivery.job_id))
                if _song is not None and _song.id in current_by_song:
                    current_ids.add(current_by_song[_song.id].job_id)
            documents = {doc.job_id: doc for doc in db.query(EditorDocument).filter(
                EditorDocument.job_id.in_(current_ids or {""})).all()}
            payload = []
            for request, delivery in page:
                song = song_of(jobs_by_id.get(delivery.job_id))
                job = current_by_song.get(song.id) if song is not None else jobs_by_id.get(delivery.job_id)
                job = job or jobs_by_id.get(delivery.job_id)
                closable, _ = closable_on_publish(request, job, delivery, documents.get(job.job_id) if job else None)
                payload.append({
                    "id": request.id, "delivery_id": delivery.id,
                    "portal_id": delivery.portal_id or "argentina",
                    "job_id": delivery.job_id, "song_id": song.id if song else None,
                    "artist": delivery.artist_snapshot, "song": delivery.song_title_snapshot,
                    "comment": request.comment,
                    "submitted_at": _iso(request.submitted_at), "updated_at": _iso(request.updated_at or request.submitted_at),
                    "resolved_at": _iso(request.resolved_at), "resolution_note": request.resolution_note,
                    "resolution_source": request.resolution_source,
                    "published_revision": delivery.published_revision or 1,
                    "client_approval": _client_approval(delivery),
                    "step": _step(request, job, delivery),
                    "current_job_id": job.job_id if job is not None else None,
                    "closable_on_publish": closable,
                })
            last = page[-1][0] if more and page else None
            next_cursor = (f"{_iso(last.updated_at or last.submitted_at)}|{last.id}" if last is not None else None)
    except HTTPException:
        raise
    except Exception:
        # The portal being unreachable must read as "unknown", never as "no requests".
        return {**empty, "available": False, "counts": {"open": None, "resolved": None, "oldest_open_at": None}}
    open_count, resolved_count, oldest_open = counted
    return {
        "campaign_id": campaign.id, "available": True,
        "counts": {"open": int(open_count or 0), "resolved": int(resolved_count or 0), "oldest_open_at": _iso(oldest_open)},
        "items": payload, "next_cursor": next_cursor,
    }


class ResolveBody(BaseModel):
    resolution_note: str = Field(default="", max_length=2000)


@router.post("/{campaign_id}/change-requests/{request_id}/resolve")
def resolve_campaign_change_request(
    campaign_id: str,
    request_id: int,
    body: ResolveBody,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    ddb: Session = Depends(get_deliveries_db),
):
    """Close ONE request of this campaign by hand, with a reason the client sees.

    Same effect as Admin > Cambios "Marcar como resuelto", for the campaign's
    owner or any admin. It states that the request is attended; it does not
    render or publish anything. The request must belong to this campaign's
    songs AND tenant, otherwise it is reported as not found.
    """
    _require_scope(current_user)
    campaign = _campaign_or_404(db, campaign_id, current_user)
    if not actions_enabled():
        raise HTTPException(status_code=404, detail={"code": "feature_disabled"})
    _require_manager(campaign, current_user)
    note = body.resolution_note.strip()
    if not note:
        raise HTTPException(status_code=422, detail={
            "code": "resolution_reason_required",
            "message": "Explicá por qué el pedido está atendido. Cerrar manualmente no publica otro video.",
        })
    lineage = {job.job_id for job in _load_lineage(db, [campaign], full=False)[campaign.id]}
    row = ddb.query(DeliveryChangeRequest, Delivery).join(
        Delivery, Delivery.id == DeliveryChangeRequest.delivery_id,
    ).filter(
        DeliveryChangeRequest.id == request_id, Delivery.tenant_snapshot == campaign.tenant_id,
        Delivery.job_id.in_(lineage or {""}),
    ).with_for_update(of=DeliveryChangeRequest).first()
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "change_request_not_found"})
    request, delivery = row
    if request.resolved_at is not None:
        # Idempotent: a double click must not read as an error.
        return {"ok": True, "already_resolved": True, "resolved_at": _iso(request.resolved_at), "updated_at": _iso(request.updated_at)}
    now = datetime.now(timezone.utc)
    request.resolved_at = now
    request.updated_at = now
    request.resolved_by_user_id = deliveries_added_by(current_user["id"])
    request.resolution_note = note
    request.resolution_source = "manual"
    ddb.commit()
    db.add(AuditLog(user_id=current_user["id"], action="delivery.change_request.resolve", detail={
        "change_request_id": request_id, "delivery_id": delivery.id, "campaign_id": campaign.id,
        "source": "campaign", "note_preview": note[:200],
    }))
    db.commit()
    return {"ok": True, "resolved_at": _iso(now), "updated_at": _iso(now)}
