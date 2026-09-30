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
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

import delivery_freshness
from auth import get_current_user
from batch_campaigns import _aware, _campaign_or_404, _require_scope
from campaign_pipeline import _load_items, _load_lineage
from database import Delivery, DeliveryChangeRequest, Job, get_db, scoped_deliveries_db

router = APIRouter(prefix="/batch/campaigns", tags=["campaign-change-requests"])

_RENDERING = frozenset({"queued", "processing", "rendering", "editing", "background_generating", "transcribed_pending"})
_FINISHED = frozenset({"done", "pending_review"})
MAX_PAGE = 200


def feature_enabled() -> bool:
    return os.environ.get("CAMPAIGN_CHANGE_REQUESTS_ENABLED", "0") == "1"


def _iso(value: datetime | None) -> str | None:
    value = _aware(value)
    return value.isoformat() if value else None


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
            payload = []
            for request, delivery in page:
                job = jobs_by_id.get(delivery.job_id)
                song = song_of(job)
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
