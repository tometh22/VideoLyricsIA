"""One stage per campaign song, computed once for every campaign screen.

Before this module each campaign tab counted a different thing: lyric
approvals, principal jobs, video rows including variants, portal rows. The
same 300-song campaign showed 103, 97, 100 and 105 "approved" depending on
the tab. The UI now reads a single song-level snapshot:

    audio → lyrics → ready → rendering → qc → approved → delivered
                                   (+ attention, discarded)

The unit is always the campaign item (the song). Variants never add songs:
the most recent live job of a song's lineage is its *current* job, and the
song is ``delivered`` only when that exact job is published in a portal.
Read-only: no status is mutated here.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from auth import get_current_user
from batch_campaigns import (
    _FAILURE,
    _aware,
    _campaign_or_404,
    _campaign_rows,
    _phase,
    _require_scope,
)
from database import BatchCampaign, Job, User, get_db

router = APIRouter(prefix="/batch/campaigns", tags=["campaign-pipeline"])

STAGES = ("audio", "lyrics", "ready", "rendering", "qc", "approved", "delivered")
EXTRA_STAGES = ("attention", "discarded")
ALL_STAGES = STAGES + EXTRA_STAGES

_PHASE_STAGE = {
    "waiting_upload": "audio",
    "uploading": "audio",
    "waiting_processing": "audio",
    "transcribing": "audio",
    "separating": "audio",
    "separation_ready": "audio",
    "lyrics_ready": "lyrics",
    "lyrics_approved": "ready",
    "rendering": "rendering",
    "final_review": "qc",
    "done": "approved",
    "failed": "attention",
    "discarded": "discarded",
}
_VIDEO_STATUSES = frozenset({
    "queued", "processing", "editing", "background_generating", "rendering",
    "pending_review", "done",
})


def stage_for(phase: str, *, published: bool) -> str:
    stage = _PHASE_STAGE.get(phase, "attention")
    if stage == "approved" and published:
        return "delivered"
    return stage


def _iso(value: datetime | None) -> str | None:
    value = _aware(value)
    return value.isoformat() if value else None


def _lineage(db: Session, campaign: BatchCampaign) -> list[Job]:
    """Principal jobs plus every tenant-scoped descendant (variants)."""
    from campaign_creative import campaign_jobs
    return campaign_jobs(db, campaign)


def _publications(job_ids: list[str], tenant_id: str) -> tuple[dict[str, dict[str, Any]], bool]:
    """Portal rows keyed by job; ``False`` when the portal DB is unreachable.

    An unreachable portal must never make the UI claim a song is delivered or
    silently undelivered, so the caller reports the uncertainty instead.
    """
    if not job_ids:
        return {}, True
    try:
        from campaign_creative import portal_history
        return portal_history(job_ids, tenant_id), True
    except Exception:  # pragma: no cover - exercised through monkeypatch
        return {}, False


def _current_fingerprint(job: Job) -> str | None:
    try:
        import delivery_freshness
        return delivery_freshness.render_fingerprint(job)
    except Exception:
        return None


def build_pipeline(db: Session, campaign: BatchCampaign, *, include_items: bool = True) -> dict[str, Any]:
    rows = _campaign_rows(db, campaign.id)
    jobs = _lineage(db, campaign)
    by_id = {job.job_id: job for job in jobs}
    principal_item = {job.job_id: job.campaign_item_id for job in jobs if job.campaign_item_id}

    def root_item(job: Job) -> str | None:
        seen: set[str] = set()
        current: Job | None = job
        while current is not None and current.job_id not in seen:
            seen.add(current.job_id)
            if current.job_id in principal_item:
                return principal_item[current.job_id]
            current = by_id.get(current.parent_job_id) if current.parent_job_id else None
        return None

    lineage_by_item: dict[str, list[Job]] = {}
    for job in jobs:
        item_id = root_item(job)
        if item_id:
            lineage_by_item.setdefault(item_id, []).append(job)

    publications, portal_available = _publications([job.job_id for job in jobs], campaign.tenant_id)
    approver_ids = {int(job.approved_by) for job in jobs if job.approved_by is not None}
    approvers = {
        row.id: (row.full_name or row.username or row.email or f"user-{row.id}")
        for row in db.query(User).filter(User.id.in_(approver_ids)).all()
    } if approver_ids else {}

    counts = {stage: 0 for stage in ALL_STAGES}
    flags = {"portal_outdated": 0, "change_requests": 0, "metadata_missing": 0, "upload_errors": 0}
    items: list[dict[str, Any]] = []
    for item, principal in rows:
        lineage = sorted(
            lineage_by_item.get(item.id, [principal] if principal else []),
            key=lambda job: (_aware(job.created_at) or datetime.min.replace(tzinfo=timezone.utc), job.job_id),
        )
        # The newest live job is what the client would receive next. A failed
        # or discarded variant must not hide an already approved original.
        if principal is None or principal.status in _FAILURE or principal.status == "discarded":
            current = principal
        else:
            live = [job for job in lineage if job.status not in _FAILURE and job.status != "discarded"]
            current = live[-1] if live else principal
        phase = _phase(item.upload_state, current.status if current else None, item.metadata_error)
        current_publication = publications.get(current.job_id, {}) if current else {}
        published_current = bool(current_publication.get("umg_portals"))
        stage = stage_for(phase, published=published_current)
        counts[stage] += 1

        portals: list[str] = []
        change_requests = 0
        outdated = False
        updating = False
        published_other = False
        for job in lineage:
            publication = publications.get(job.job_id)
            if not publication:
                continue
            for portal in publication.get("umg_portals", []):
                if portal not in portals:
                    portals.append(portal)
            change_requests += int(publication.get("pending_change_requests") or 0)
            updating = updating or bool(publication.get("portal_updating"))
            if current is not None and job.job_id != current.job_id:
                published_other = True
            fingerprints = publication.get("published_fingerprints") or []
            fingerprint = _current_fingerprint(job)
            if fingerprints and fingerprint and any(fp != fingerprint for fp in fingerprints):
                outdated = True
        portals.sort()
        if outdated:
            flags["portal_outdated"] += 1
        if change_requests:
            flags["change_requests"] += 1
        if item.metadata_error == "missing_metadata":
            flags["metadata_missing"] += 1
        if item.upload_state == "error":
            flags["upload_errors"] += 1
        if not include_items:
            continue
        # Every variant is a version (a failed re-render is part of the song's
        # history); the principal only once it produced or is producing video.
        videos = [
            job for job in lineage
            if job.video_url or job.status in _VIDEO_STATUSES
            or (principal is not None and job.job_id != principal.job_id)
        ]
        items.append({
            "item_id": item.id,
            "ordinal": item.ordinal,
            "title": item.title or item.filename,
            "artist": item.artist or "",
            "technical_code": item.technical_code,
            "filename": item.filename,
            "duration_seconds": item.duration_seconds,
            "stage": stage,
            "phase": phase,
            "job_id": principal.job_id if principal else None,
            "job_status": principal.status if principal else None,
            "current_job_id": current.job_id if current else None,
            "current_status": current.status if current else None,
            "current_is_variant": bool(current and principal and current.job_id != principal.job_id),
            "upload_state": item.upload_state,
            "upload_error": item.upload_error,
            "metadata_error": item.metadata_error,
            "cover_match_state": item.cover_match_state,
            "association_confirmed": bool(item.association_confirmed),
            "discard": item.discard_record if stage == "discarded" else None,
            "registered_at": _iso(item.created_at),
            "uploaded_at": _iso(item.uploaded_at),
            "approved_at": _iso(current.approved_at) if current else None,
            "approved_by_name": approvers.get(current.approved_by) if current and current.approved_by else None,
            "updated_at": _iso(current.updated_at) if current and getattr(current, "updated_at", None) else None,
            "error": (current.error or None) if current and stage == "attention" else None,
            "video_count": len(videos),
            "has_video": bool(current and current.video_url),
            "versions": [
                {
                    "job_id": job.job_id,
                    "status": job.status,
                    "is_variant": bool(principal and job.job_id != principal.job_id),
                    "created_at": _iso(job.created_at),
                    "approved_at": _iso(job.approved_at),
                    "has_video": bool(job.video_url),
                    "portals": sorted(publications.get(job.job_id, {}).get("umg_portals", [])),
                }
                for job in videos
            ],
            "portals": portals,
            "portal_outdated": outdated,
            "portal_updating": updating,
            "published_other_version": published_other and not published_current,
            "pending_change_requests": change_requests,
        })

    total = len(rows)
    return {
        "campaign_id": campaign.id,
        "kind": campaign.kind or "lyric_video",
        "total": total,
        "active_total": total - counts["discarded"],
        "counts": counts,
        "flags": flags,
        "portal_status_available": portal_available,
        "stages": list(STAGES),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        **({"items": items} if include_items else {}),
    }


def pipeline_counts(db: Session, campaign: BatchCampaign) -> dict[str, Any] | None:
    """Compact snapshot for list cards; never breaks the list on failure."""
    try:
        snapshot = build_pipeline(db, campaign, include_items=False)
    except Exception:
        return None
    return {key: snapshot[key] for key in ("counts", "flags", "total", "active_total", "portal_status_available")}


@router.get("/{campaign_id}/pipeline")
def campaign_pipeline(
    campaign_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_scope(current_user)
    campaign = _campaign_or_404(db, campaign_id, current_user)
    snapshot = build_pipeline(db, campaign)
    snapshot["can_manage"] = current_user.get("role") == "admin" or campaign.created_by == current_user.get("id")
    return snapshot
