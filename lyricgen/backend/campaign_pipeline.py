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

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable

from fastapi import APIRouter, Depends
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, load_only

from auth import get_current_user
from batch_campaigns import (
    _FAILURE,
    _aware,
    _campaign_or_404,
    _phase,
    _require_scope,
)
from database import BatchCampaign, BatchCampaignItem, Job, User, get_db

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
# Enough to derive stages; the heavy JSON columns (segments, quality) are
# never needed for counts.
_JOB_COLUMNS = (
    Job.job_id, Job.tenant_id, Job.status, Job.campaign_id, Job.campaign_item_id,
    Job.parent_job_id, Job.pilot_id, Job.created_at, Job.approved_at, Job.approved_by,
    Job.video_url, Job.error,
)
_ITEM_COLUMNS = (
    BatchCampaignItem.id, BatchCampaignItem.campaign_id, BatchCampaignItem.ordinal,
    BatchCampaignItem.filename, BatchCampaignItem.title, BatchCampaignItem.artist,
    BatchCampaignItem.technical_code, BatchCampaignItem.duration_seconds,
    BatchCampaignItem.upload_state, BatchCampaignItem.upload_error,
    BatchCampaignItem.metadata_error, BatchCampaignItem.cover_match_state,
    BatchCampaignItem.association_confirmed, BatchCampaignItem.discard_record,
    BatchCampaignItem.created_at, BatchCampaignItem.uploaded_at,
)


def stage_for(phase: str, *, published: bool) -> str:
    stage = _PHASE_STAGE.get(phase, "attention")
    if stage == "approved" and published:
        return "delivered"
    return stage


def _iso(value: datetime | None) -> str | None:
    value = _aware(value)
    return value.isoformat() if value else None


def _load_items(db: Session, campaign_ids: list[str]) -> dict[str, list[BatchCampaignItem]]:
    items: dict[str, list[BatchCampaignItem]] = defaultdict(list)
    if not campaign_ids:
        return items
    for item in db.query(BatchCampaignItem).options(load_only(*_ITEM_COLUMNS)).filter(
        BatchCampaignItem.campaign_id.in_(campaign_ids),
    ).order_by(BatchCampaignItem.campaign_id, BatchCampaignItem.ordinal.asc()).all():
        items[item.campaign_id].append(item)
    return items


def _load_lineage(db: Session, campaigns: list[BatchCampaign], *, full: bool) -> dict[str, list[Job]]:
    """Principal jobs plus every same-tenant descendant (variants), per campaign.

    One recursive query for all campaigns. Pilot test copies (``pilot_id``)
    are private rehearsal jobs and never become a song's current cut.
    """
    result: dict[str, list[Job]] = defaultdict(list)
    if not campaigns:
        return result
    tenant_of = {campaign.id: campaign.tenant_id for campaign in campaigns}
    root = select(Job.job_id, Job.tenant_id, Job.campaign_id.label("root_campaign")).where(
        Job.campaign_id.in_(list(tenant_of)),
    ).cte(recursive=True)
    child = select(Job.job_id, Job.tenant_id, root.c.root_campaign).join(
        root, Job.parent_job_id == root.c.job_id,
    ).where(
        Job.tenant_id == root.c.tenant_id,
        or_(Job.campaign_id.is_(None), Job.campaign_id == root.c.root_campaign),
    )
    lineage = root.union(child)
    pairs = db.execute(select(lineage.c.job_id, lineage.c.tenant_id, lineage.c.root_campaign)).all()
    wanted = {job_id: campaign_id for job_id, tenant_id, campaign_id in pairs if tenant_of.get(campaign_id) == tenant_id}
    if not wanted:
        return result
    query = db.query(Job).filter(Job.job_id.in_(list(wanted)), Job.pilot_id.is_(None))
    if not full:
        query = query.options(load_only(*_JOB_COLUMNS))
    for job in query.all():
        campaign_id = wanted.get(job.job_id)
        if campaign_id and job.tenant_id == tenant_of[campaign_id]:
            result[campaign_id].append(job)
    return result


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
    except Exception:
        return {}, False


def _current_fingerprint(job: Job) -> str | None:
    try:
        import delivery_freshness
        return delivery_freshness.render_fingerprint(job)
    except Exception:
        return None


def _snapshot(
    campaign: BatchCampaign,
    items: list[BatchCampaignItem],
    jobs: list[Job],
    publications: dict[str, dict[str, Any]],
    portal_available: bool,
    *,
    include_items: bool,
    approvers: dict[int, str] | None = None,
) -> dict[str, Any]:
    kind = campaign.kind or "lyric_video"
    by_id = {job.job_id: job for job in jobs}
    principal_of_item = {
        job.campaign_item_id: job for job in jobs
        if job.campaign_item_id and job.campaign_id == campaign.id
    }
    principal_item = {job.job_id: item_id for item_id, job in principal_of_item.items()}

    def root_item(job: Job) -> str | None:
        seen: set[str] = set()
        current: Job | None = job
        while current is not None and current.job_id not in seen:
            seen.add(current.job_id)
            if current.job_id in principal_item:
                return principal_item[current.job_id]
            current = by_id.get(current.parent_job_id) if current.parent_job_id else None
        return None

    lineage_by_item: dict[str, list[Job]] = defaultdict(list)
    for job in jobs:
        item_id = root_item(job)
        if item_id:
            lineage_by_item[item_id].append(job)

    from delivery_snapshots import latest_pointer_enabled
    serves_latest = latest_pointer_enabled()
    counts = {stage: 0 for stage in ALL_STAGES}
    flags: dict[str, Any] = {
        "portal_outdated": 0, "change_requests": 0, "metadata_missing": 0, "upload_errors": 0,
        # Open client requests (not songs) and how long the oldest has waited:
        # what the campaign list needs to say "3 cambios · el más antiguo hace 2 días".
        "change_requests_open": 0, "oldest_change_request_at": None,
    }
    oldest_request: datetime | None = None
    rows: list[dict[str, Any]] = []
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    for item in items:
        principal = principal_of_item.get(item.id)
        lineage = sorted(
            lineage_by_item.get(item.id) or ([principal] if principal else []),
            key=lambda job: (_aware(job.created_at) or epoch, job.job_id),
        )
        # The newest live job is what the client would receive next. A failed
        # or discarded variant must not hide an already approved original.
        if principal is None or principal.status in _FAILURE or principal.status == "discarded":
            current = principal
        else:
            live = [job for job in lineage if job.status not in _FAILURE and job.status != "discarded"]
            current = live[-1] if live else principal
        phase = _phase(item.upload_state, current.status if current else None, item.metadata_error)
        published_current = bool(current and publications.get(current.job_id, {}).get("umg_portals"))
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
            published_oldest = _aware(publication.get("oldest_change_request_at"))
            if published_oldest is not None and (oldest_request is None or published_oldest < oldest_request):
                oldest_request = published_oldest
            updating = updating or bool(publication.get("portal_updating"))
            if current is not None and job.job_id != current.job_id:
                published_other = True
            if include_items:
                fingerprints = publication.get("published_fingerprints") or []
                fingerprint = _current_fingerprint(job)
                if fingerprints and fingerprint and any(fp != fingerprint for fp in fingerprints):
                    outdated = True
        portals.sort()
        flags["portal_outdated"] += int(outdated)
        flags["change_requests"] += int(bool(change_requests))
        flags["change_requests_open"] += change_requests
        flags["metadata_missing"] += int(item.metadata_error == "missing_metadata")
        flags["upload_errors"] += int(item.upload_state == "error")
        if not include_items:
            continue
        # Every variant is a version (a failed re-render is part of the song's
        # history); the principal only once it produced or is producing video.
        versions = [
            job for job in lineage
            if job.video_url or job.status in _VIDEO_STATUSES
            or (principal is not None and job.job_id != principal.job_id)
        ]
        # Portal sends select campaign jobs (lyric videos) or the item's
        # principal job (art tracks): a pre-feature variant outside the
        # campaign cannot be sent from here.
        sendable = bool(
            current and current.campaign_id == campaign.id
            and (kind != "art_track" or current is principal)
        )
        rows.append({
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
            "current_sendable": sendable,
            "upload_state": item.upload_state,
            "upload_error": item.upload_error,
            "metadata_error": item.metadata_error,
            "cover_match_state": item.cover_match_state,
            "association_confirmed": bool(item.association_confirmed),
            "discard": item.discard_record if stage == "discarded" else None,
            "registered_at": _iso(item.created_at),
            "uploaded_at": _iso(item.uploaded_at),
            "approved_at": _iso(current.approved_at) if current else None,
            "approved_by_name": (approvers or {}).get(current.approved_by) if current and current.approved_by else None,
            "error": (current.error or None) if current and stage == "attention" else None,
            "video_count": len(versions),
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
                for job in versions
            ],
            "portals": portals,
            "portal_outdated": outdated,
            # In pointer mode the portal already serves the newest render, so an
            # 'outdated' song means "registration pending", not "client sees old".
            "portal_serves_latest": serves_latest,
            "portal_updating": updating,
            "published_other_version": published_other and not published_current,
            "pending_change_requests": change_requests,
        })

    flags["oldest_change_request_at"] = _iso(oldest_request)
    total = len(items)
    publication_mode = "pointer" if serves_latest else "snapshot"
    snapshot: dict[str, Any] = {
        "campaign_id": campaign.id,
        "kind": kind,
        "total": total,
        "active_total": total - counts["discarded"],
        "counts": counts,
        "flags": flags,
        "portal_status_available": portal_available,
        "publication_mode": publication_mode,
        "stages": list(STAGES),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    if include_items:
        snapshot["items"] = rows
    return snapshot


def build_pipeline(db: Session, campaign: BatchCampaign, *, include_items: bool = True) -> dict[str, Any]:
    items = _load_items(db, [campaign.id])[campaign.id]
    jobs = _load_lineage(db, [campaign], full=include_items)[campaign.id]
    publications, portal_available = _publications([job.job_id for job in jobs], campaign.tenant_id)
    approver_ids = {int(job.approved_by) for job in jobs if job.approved_by is not None} if include_items else set()
    approvers = {
        row.id: (row.full_name or row.username or row.email or f"user-{row.id}")
        for row in db.query(User).filter(User.id.in_(approver_ids)).all()
    } if approver_ids else {}
    return _snapshot(campaign, items, jobs, publications, portal_available, include_items=include_items, approvers=approvers)


def pipeline_counts_bulk(db: Session, campaigns: Iterable[BatchCampaign]) -> dict[str, dict[str, Any]]:
    """Counts for list cards with a fixed number of queries for any number of
    campaigns: one item query, one lineage query and one portal query per
    tenant. Never breaks the list: on failure the session is rolled back and
    the caller keeps the phase counters it already has.
    """
    campaigns = list(campaigns)
    try:
        items = _load_items(db, [campaign.id for campaign in campaigns])
        lineage = _load_lineage(db, campaigns, full=False)
        jobs_by_tenant: dict[str, list[str]] = defaultdict(list)
        for campaign in campaigns:
            jobs_by_tenant[campaign.tenant_id].extend(job.job_id for job in lineage[campaign.id])
        publications: dict[str, dict[str, Any]] = {}
        available_by_tenant: dict[str, bool] = {}
        for tenant_id, job_ids in jobs_by_tenant.items():
            found, available = _publications(job_ids, tenant_id)
            publications.update(found)
            available_by_tenant[tenant_id] = available
        result = {}
        for campaign in campaigns:
            snapshot = _snapshot(
                campaign, items[campaign.id], lineage[campaign.id], publications,
                available_by_tenant.get(campaign.tenant_id, True), include_items=False,
            )
            result[campaign.id] = {
                key: snapshot[key]
                for key in ("counts", "flags", "total", "active_total", "portal_status_available")
            }
        return result
    except Exception:
        db.rollback()
        return {}


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
    from campaign_change_requests import feature_enabled as change_requests_inbox_enabled
    snapshot["is_admin"] = current_user.get("role") == "admin"
    from campaign_change_requests import actions_enabled as change_request_actions_enabled
    snapshot["features"] = {
        "change_requests_inbox": change_requests_inbox_enabled(),
        "change_request_actions": change_request_actions_enabled(),
    }
    return snapshot
