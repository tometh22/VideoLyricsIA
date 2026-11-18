"""Personal campaign production, attributed to the operator's accepted request.

Job.user_id is the account owner, not necessarily the operator. Count a job's
first successful render once; edits, retries and output formats are not songs.
"""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from auth import get_current_user
from batch_campaigns import _aware, _campaign_or_404, _require_scope
from database import AuditLog, Job, get_db

router = APIRouter(prefix="/batch/activity", tags=["campaigns"])
ZONE = ZoneInfo("America/Argentina/Buenos_Aires")
GENERATION = "campaign.creative.generation"
RENDERED = "campaign.creative.rendered"


def day_bounds(day):
    start = datetime.combine(day, time.min, ZONE)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def collect(db, user, day):
    _require_scope(user)
    start, end = day_bounds(day)
    job_key = AuditLog.detail["job_id"].as_string()
    # Project only IDs/timestamps: audit details can contain entire lyrics.
    owned_ids = db.query(job_key).filter(
        AuditLog.user_id == user["id"], AuditLog.action == GENERATION,
        AuditLog.created_at < end,
    ).distinct().subquery()
    requests = db.query(job_key.label("job_id"), AuditLog.user_id, AuditLog.created_at, AuditLog.id).filter(
        AuditLog.action == GENERATION, job_key.in_(db.query(owned_ids)),
        AuditLog.created_at < end,
    ).order_by(AuditLog.created_at, AuditLog.id).all()
    first = {}
    for r in requests:
        first.setdefault(r.job_id, r)
    edits = db.query(job_key).filter(
        AuditLog.user_id == user["id"], AuditLog.action == "job.edit_request",
        AuditLog.created_at >= start, AuditLog.created_at < end,
    ).all()
    edit_ids = {r[0] for r in edits if r[0]}
    ids = set(first) | edit_ids
    jobs = db.query(Job.job_id, Job.campaign_id, Job.artist, Job.song_title, Job.status,
                    Job.parent_job_id).filter(Job.job_id.in_(ids)).all() if ids else []
    rendered = dict(db.query(job_key, func.min(AuditLog.created_at)).filter(
        AuditLog.action == RENDERED, job_key.in_(list(first)),
    ).group_by(job_key).all()) if first else {}
    # The accepted request immediately preceding the first successful render
    # owns that conversion. A different operator may have retried a failed run.
    rendered_by = {}
    for r in requests:
        finished = rendered.get(r.job_id)
        if finished and _aware(r.created_at) <= _aware(finished):
            rendered_by[r.job_id] = r.user_id
    campaigns, rows, visible_edits = {}, [], set()
    requested = 0
    for j in jobs:
        if not j.campaign_id or j.parent_job_id:
            continue
        if j.campaign_id not in campaigns:
            try:
                campaigns[j.campaign_id] = _campaign_or_404(db, j.campaign_id, user)
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
                campaigns[j.campaign_id] = None
        campaign = campaigns[j.campaign_id]
        if campaign is None:
            continue
        if j.job_id in edit_ids:
            visible_edits.add(j.job_id)
        request = first.get(j.job_id)
        if not request:
            continue
        if request.user_id == user["id"] and start <= _aware(request.created_at) < end:
            requested += 1
        finished = rendered.get(j.job_id)
        if not finished or not start <= _aware(finished) < end or rendered_by.get(j.job_id) != user["id"]:
            continue
        rows.append({"job_id": j.job_id, "campaign_id": campaign.id,
                     "campaign_name": campaign.name, "portal_id": campaign.destination_portal,
                     "artist": j.artist, "title": j.song_title or "Sin título",
                     "status": j.status, "completed_at": _aware(finished).isoformat()})
    rows.sort(key=lambda r: (r["campaign_name"], r["artist"] or "", r["title"], r["job_id"]))
    groups = {}
    for r in rows:
        group = groups.setdefault(r["campaign_id"], {"id": r["campaign_id"], "name": r["campaign_name"], "count": 0})
        group["count"] += 1
    return {"day": day.isoformat(), "timezone": str(ZONE), "generated": len(rows),
            "requested": requested, "edited_songs": len(visible_edits),
            "edit_requests": sum(r[0] in visible_edits for r in edits),
            "approved_now": sum(r["status"] in ("done", "approved") for r in rows),
            "pending_review_now": sum(r["status"] == "pending_review" for r in rows),
            "campaigns": list(groups.values()), "songs": rows,
            "coverage": "campaign_render_receipts"}


@router.get("/me")
def my_activity(day: date | None = None, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    return collect(db, current_user, day or datetime.now(ZONE).date())
