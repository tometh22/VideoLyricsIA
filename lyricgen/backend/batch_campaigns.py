"""Durable high-volume campaign ingestion and orchestration.

The local uploader only moves bytes. This module owns campaign state,
tenant scoping, bounded promotion into the existing transcription pipeline,
and per-tab review claims. No background generation happens here.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from auth import get_current_user
from database import (
    AuditLog,
    BatchCampaign,
    BatchCampaignItem,
    BatchUploadSession,
    EditorDocument,
    Job,
    ProductEvent,
    SessionLocal,
    User,
    get_db,
)
from jobs import create_job
from machine_evidence import MachineSnapshotMissing
import storage


router = APIRouter(prefix="/batch", tags=["batch-campaigns"])

CAMPAIGN_STATUSES = frozenset({"active", "paused", "completed", "cancelled"})
ITEM_LIMIT = int(os.environ.get("BATCH_CAMPAIGN_ITEM_LIMIT", "1000"))
TRANSCRIPTION_WINDOW = int(os.environ.get("BATCH_TRANSCRIPTION_WINDOW", "30"))
# The conservative platform default remains 50.  Large campaigns must opt in
# explicitly through stage1_pipeline.lyrics_ready_limit; this avoids silently
# widening every tenant while still allowing the 300-song August queue.
LYRICS_READY_LIMIT = int(os.environ.get("BATCH_LYRICS_READY_LIMIT", "50"))
SEPARATION_WINDOW = int(os.environ.get("BATCH_SEPARATION_WINDOW", "300"))
RENDER_WINDOW = int(os.environ.get("BATCH_RENDER_WINDOW", "10"))
FINAL_REVIEW_LIMIT = int(os.environ.get("BATCH_FINAL_REVIEW_LIMIT", "50"))
PART_SIZE = int(os.environ.get("MULTIPART_PART_SIZE_BYTES", str(8 * 1024 * 1024)))
MULTIPART_THRESHOLD = int(os.environ.get("MULTIPART_THRESHOLD_BYTES", str(16 * 1024 * 1024)))
PAIR_TTL_MINUTES = int(os.environ.get("BATCH_PAIR_CODE_TTL_MINUTES", "10"))
UPLOAD_TOKEN_HOURS = int(os.environ.get("BATCH_UPLOAD_TOKEN_HOURS", "12"))
RECONCILE_SECONDS = int(os.environ.get("BATCH_RECONCILE_SECONDS", "30"))
MAX_AUDIO_BYTES = int(os.environ.get("BATCH_MAX_AUDIO_BYTES", str(500 * 1024 * 1024)))
MAX_AUDIO_DURATION = float(os.environ.get("BATCH_MAX_AUDIO_DURATION", "3600"))

_ACTIVE_TRANSCRIPTION = frozenset({"awaiting_upload", "transcribing_queued", "transcribing"})
_ACTIVE_SEPARATION = frozenset({"separation_queued", "separating"})
_ACTIVE_RENDER = frozenset({"queued", "processing", "editing", "background_generating", "rendering"})
_FAILURE = frozenset({"error", "transcription_failed", "validation_failed", "rejected"})
_SAFE_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _latest_semaforo_verdicts(
    db: Session,
    job_ids: list[str],
) -> dict[str, dict[str, Any]]:
    wanted = set(job_ids)
    if not wanted:
        return {}
    verdicts: dict[str, dict[str, Any]] = {}
    for log in db.query(AuditLog).filter(
        AuditLog.action.in_(("semaforo.verdict.v2", "semaforo.verdict.v1")),
    ).order_by(AuditLog.id.desc()).all():
        detail = dict(log.detail or {})
        verdict_job = str(detail.get("job_id") or "")
        if verdict_job in wanted and verdict_job not in verdicts:
            verdicts[verdict_job] = detail
    return verdicts


def _delivery_rank(
    item: BatchCampaignItem,
    verdict: dict[str, Any] | None,
) -> tuple[int, int, float, int]:
    title = f"{item.title or ''} {item.filename or ''}".lower()
    is_live = "live" in title or "en vivo" in title
    color = str((verdict or {}).get("color") or "red").lower()
    color_rank = {"green": 0, "yellow": 1, "red": 2}.get(color, 2)
    signal_rank = _number((verdict or {}).get("rank_key"), 9_999.0)
    return (1 if is_live else 0, color_rank, signal_rank, int(item.ordinal or 0))


def feature_enabled() -> bool:
    return os.environ.get("BATCH_CAMPAIGN_ENABLED", "0").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _scope_enabled(user: dict) -> bool:
    if user.get("role") == "admin":
        return feature_enabled()
    allowed = {
        value.strip().lower()
        for value in os.environ.get("BATCH_CAMPAIGN_SCOPES", "").split(",")
        if value.strip()
    }
    scopes = {
        str(user.get("tenant_id") or "").strip().lower(),
        str(user.get("billing_group") or "").strip().lower(),
    }
    return feature_enabled() and bool((scopes - {""}) & allowed)


def _require_scope(user: dict) -> None:
    if not _scope_enabled(user):
        raise HTTPException(status_code=404, detail="Batch campaigns are not enabled.")


def _campaign_or_404(db: Session, campaign_id: str, user: dict) -> BatchCampaign:
    query = db.query(BatchCampaign).filter(BatchCampaign.id == campaign_id)
    if user.get("role") != "admin":
        query = query.filter(BatchCampaign.tenant_id == user["tenant_id"])
    campaign = query.first()
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found.")
    return campaign


def _require_manager(campaign: BatchCampaign, user: dict) -> None:
    if user.get("role") != "admin" and campaign.created_by != user.get("id"):
        raise HTTPException(status_code=403, detail="Only the campaign owner can change it.")


def _hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _phase(upload_state: str, job_status: str | None, metadata_error: str | None = None) -> str:
    if job_status in _ACTIVE_SEPARATION:
        return "separating"
    if job_status == "separation_ready":
        return "separation_ready"
    if job_status in _ACTIVE_TRANSCRIPTION:
        return "transcribing"
    if job_status in {"transcribed_pending", "transcribed"}:
        return "lyrics_ready"
    if job_status == "lyrics_approved":
        return "lyrics_approved"
    if job_status in _ACTIVE_RENDER:
        return "rendering"
    if job_status == "pending_review":
        return "final_review"
    if job_status == "done":
        return "done"
    if job_status in _FAILURE or upload_state == "error" or metadata_error in {
        "invalid_size", "invalid_duration", "promotion_failed",
    }:
        return "failed"
    if upload_state == "uploaded":
        return "waiting_processing"
    if upload_state == "uploading":
        return "uploading"
    return "waiting_upload"


def _campaign_rows(db: Session, campaign_id: str) -> list[tuple[BatchCampaignItem, Job | None]]:
    return db.query(BatchCampaignItem, Job).outerjoin(
        Job, Job.campaign_item_id == BatchCampaignItem.id,
    ).filter(BatchCampaignItem.campaign_id == campaign_id).order_by(
        BatchCampaignItem.ordinal.asc(),
    ).all()


def _summary(db: Session, campaign: BatchCampaign) -> dict[str, Any]:
    counters = {
        key: 0 for key in (
            "waiting_upload", "uploading", "waiting_processing", "transcribing",
            "separating", "separation_ready",
            "lyrics_ready", "lyrics_approved", "rendering", "final_review",
            "done", "failed",
        )
    }
    rows = _campaign_rows(db, campaign.id)
    for item, job in rows:
        counters[_phase(item.upload_state, job.status if job else None, item.metadata_error)] += 1
    return {
        "id": campaign.id,
        "name": campaign.name,
        "status": campaign.status,
        "created_by": campaign.created_by,
        "expected_count": campaign.expected_count,
        "registered_count": len(rows),
        "default_render_params": campaign.default_render_params or {},
        "counters": counters,
        "created_at": campaign.created_at.isoformat() if campaign.created_at else None,
        "updated_at": campaign.updated_at.isoformat() if campaign.updated_at else None,
        "completed_at": campaign.completed_at.isoformat() if campaign.completed_at else None,
    }


class CampaignCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=160)
    expected_count: int = Field(default=0, ge=0, le=ITEM_LIMIT)
    default_render_params: dict[str, Any] = Field(default_factory=dict)


class CampaignPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    status: str | None = None
    default_render_params: dict[str, Any] | None = None


class ManifestItem(BaseModel):
    client_id: str | None = Field(default=None, max_length=100)
    filename: str = Field(..., min_length=1, max_length=500)
    title: str | None = Field(default=None, max_length=500)
    artist: str | None = Field(default=None, max_length=255)
    technical_code: str | None = Field(default=None, max_length=64)
    size_bytes: int = Field(..., gt=0)
    duration_seconds: float | None = Field(default=None, ge=0)
    sha256: str = Field(..., min_length=64, max_length=64)
    metadata_error: str | None = Field(default=None, max_length=255)


class ManifestRequest(BaseModel):
    items: list[ManifestItem] = Field(..., min_length=1, max_length=100)


class PairExchange(BaseModel):
    campaign_id: str = Field(..., min_length=12, max_length=12)
    code: str = Field(..., min_length=8, max_length=32)


class UploadComplete(BaseModel):
    parts: list[dict[str, Any]] = Field(default_factory=list, max_length=10000)


class ItemPatch(BaseModel):
    title: str | None = Field(default=None, max_length=500)
    artist: str | None = Field(default=None, max_length=255)
    technical_code: str | None = Field(default=None, max_length=64)
    render_overrides: dict[str, Any] | None = None


class RetryItemResponse(BaseModel):
    job_id: str | None = None
    status: str


class LyricsApprovalRequest(BaseModel):
    editor_revision: int = Field(..., ge=0)
    editor_version_id: str | None = Field(default=None, max_length=36)
    confirmed_line_ids: list[str] = Field(..., min_length=1, max_length=2000)
    review_scope: Literal["song"] = "song"
    lyrics_confirmed: bool
    timings_confirmed: bool
    heard_against_audio: bool


@router.get("/campaigns/access")
def campaign_access(current_user: dict = Depends(get_current_user)):
    return {"enabled": _scope_enabled(current_user), "item_limit": ITEM_LIMIT}


@router.post("/campaigns")
def create_campaign(
    body: CampaignCreate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_scope(current_user)
    campaign = BatchCampaign(
        id=uuid.uuid4().hex[:12],
        tenant_id=current_user["tenant_id"],
        created_by=current_user["id"],
        name=body.name.strip(),
        expected_count=body.expected_count,
        status="active",
        default_render_params=body.default_render_params or {},
        created_at=_now(),
        updated_at=_now(),
    )
    db.add(campaign)
    db.commit()
    return _summary(db, campaign)


@router.get("/campaigns")
def list_campaigns(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_scope(current_user)
    query = db.query(BatchCampaign)
    if current_user.get("role") != "admin":
        query = query.filter(BatchCampaign.tenant_id == current_user["tenant_id"])
    rows = query.order_by(BatchCampaign.created_at.desc()).limit(100).all()
    return {"items": [_summary(db, row) for row in rows]}


@router.get("/campaigns/{campaign_id}")
def get_campaign(
    campaign_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_scope(current_user)
    return _summary(db, _campaign_or_404(db, campaign_id, current_user))


@router.patch("/campaigns/{campaign_id}")
def patch_campaign(
    campaign_id: str,
    body: CampaignPatch,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_scope(current_user)
    campaign = _campaign_or_404(db, campaign_id, current_user)
    _require_manager(campaign, current_user)
    if body.name is not None:
        campaign.name = body.name.strip()
    if body.default_render_params is not None:
        campaign.default_render_params = body.default_render_params
    if body.status is not None:
        if body.status not in CAMPAIGN_STATUSES:
            raise HTTPException(status_code=400, detail="Invalid campaign status.")
        if campaign.status == "cancelled" and body.status != "cancelled":
            raise HTTPException(status_code=409, detail="Cancelled campaigns cannot be resumed.")
        campaign.status = body.status
        if body.status == "completed" and campaign.completed_at is None:
            campaign.completed_at = _now()
    campaign.updated_at = _now()
    db.commit()
    if campaign.status == "active":
        ensure_campaign_reconciler_scheduled()
    return _summary(db, campaign)


@router.get("/campaigns/{campaign_id}/items")
def list_campaign_items(
    campaign_id: str,
    phase: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_scope(current_user)
    _campaign_or_404(db, campaign_id, current_user)
    rows = _campaign_rows(db, campaign_id)
    serialized = []
    for item, job in rows:
        item_phase = _phase(item.upload_state, job.status if job else None, item.metadata_error)
        if phase and item_phase != phase:
            continue
        serialized.append({
            "id": item.id,
            "ordinal": item.ordinal,
            "filename": item.filename,
            "title": item.title,
            "artist": item.artist,
            "technical_code": item.technical_code,
            "size_bytes": item.size_bytes,
            "duration_seconds": item.duration_seconds,
            "sha256": item.sha256,
            "metadata_error": item.metadata_error,
            "upload_state": item.upload_state,
            "upload_error": item.upload_error,
            "phase": item_phase,
            "job_id": job.job_id if job else None,
            "job_status": job.status if job else None,
            "render_overrides": item.render_overrides or {},
        })
    total = len(serialized)
    start = (page - 1) * limit
    return {
        "items": serialized[start:start + limit],
        "page": page,
        "limit": limit,
        "total": total,
        "pages": max(1, math.ceil(total / limit)),
    }


@router.patch("/campaigns/{campaign_id}/items/{item_id}")
def patch_campaign_item(
    campaign_id: str,
    item_id: str,
    body: ItemPatch,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_scope(current_user)
    campaign = _campaign_or_404(db, campaign_id, current_user)
    item = db.query(BatchCampaignItem).filter(
        BatchCampaignItem.id == item_id,
        BatchCampaignItem.campaign_id == campaign.id,
    ).first()
    if item is None:
        raise HTTPException(status_code=404, detail="Campaign item not found.")
    for attr in ("title", "artist", "technical_code"):
        value = getattr(body, attr)
        if value is not None:
            normalized = value.strip() or None
            if attr == "technical_code" and normalized:
                normalized = normalized.upper()
            setattr(item, attr, normalized)
    if body.render_overrides is not None:
        item.render_overrides = body.render_overrides
    if item.size_bytes <= 0 or item.size_bytes > MAX_AUDIO_BYTES:
        item.metadata_error = "invalid_size"
    elif item.duration_seconds is not None and (
        item.duration_seconds <= 0 or item.duration_seconds > MAX_AUDIO_DURATION
    ):
        item.metadata_error = "invalid_duration"
    else:
        item.metadata_error = None if item.title and item.artist and item.technical_code else "missing_metadata"
    item.updated_at = _now()
    db.commit()
    return {"ok": True, "metadata_error": item.metadata_error}


@router.post("/campaigns/{campaign_id}/upload-session")
def create_upload_session(
    campaign_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_scope(current_user)
    campaign = _campaign_or_404(db, campaign_id, current_user)
    _require_manager(campaign, current_user)
    # 48 bits, short enough to type but impractical to brute-force during
    # the ten-minute exchange window. The account JWT never leaves the tab.
    code = secrets.token_hex(6).upper()
    session = BatchUploadSession(
        id=str(uuid.uuid4()),
        campaign_id=campaign.id,
        tenant_id=campaign.tenant_id,
        created_by=current_user["id"],
        code_hash=_hash_secret(code),
        code_expires_at=_now() + timedelta(minutes=PAIR_TTL_MINUTES),
        created_at=_now(),
    )
    db.add(session)
    db.commit()
    return {
        "campaign_id": campaign.id,
        "pairing_code": code,
        "expires_in": PAIR_TTL_MINUTES * 60,
    }


@router.post("/upload-sessions/exchange")
def exchange_upload_session(body: PairExchange, db: Session = Depends(get_db)):
    now = _now()
    session = db.query(BatchUploadSession).filter(
        BatchUploadSession.campaign_id == body.campaign_id,
        BatchUploadSession.code_hash == _hash_secret(body.code.strip().upper()),
    ).with_for_update().first()
    if (
        session is None
        or session.revoked_at is not None
        or _aware(session.code_expires_at) <= now
        or session.claimed_at is not None
    ):
        raise HTTPException(status_code=401, detail="Pairing code is invalid or expired.")
    token = secrets.token_urlsafe(32)
    session.token_hash = _hash_secret(token)
    session.token_expires_at = now + timedelta(hours=UPLOAD_TOKEN_HOURS)
    session.claimed_at = now
    db.commit()
    return {"upload_token": token, "expires_in": UPLOAD_TOKEN_HOURS * 3600}


def _upload_session_or_401(
    db: Session,
    token: str | None,
    campaign_id: str | None = None,
) -> BatchUploadSession:
    if not token:
        raise HTTPException(status_code=401, detail="Missing batch upload token.")
    session = db.query(BatchUploadSession).filter(
        BatchUploadSession.token_hash == _hash_secret(token),
    ).first()
    if (
        session is None
        or session.revoked_at is not None
        or not session.token_expires_at
        or _aware(session.token_expires_at) <= _now()
        or (campaign_id and session.campaign_id != campaign_id)
    ):
        raise HTTPException(status_code=401, detail="Batch upload token is invalid or expired.")
    return session


@router.get("/upload-sessions/me")
def inspect_upload_session(
    x_batch_upload_token: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    """Read-only runner preflight and forced-expiry recovery target."""
    session = _upload_session_or_401(db, x_batch_upload_token)
    return {
        "campaign_id": session.campaign_id,
        "tenant_id": session.tenant_id,
        "expires_at": _aware(session.token_expires_at).isoformat(),
        "renewable": True,
    }


@router.post("/campaigns/{campaign_id}/manifest")
def register_manifest(
    campaign_id: str,
    body: ManifestRequest,
    x_batch_upload_token: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    session = _upload_session_or_401(db, x_batch_upload_token, campaign_id)
    campaign = db.query(BatchCampaign).filter(BatchCampaign.id == campaign_id).first()
    if campaign is None or campaign.status == "cancelled":
        raise HTTPException(status_code=409, detail="Campaign is unavailable.")
    existing_count = db.query(func.count(BatchCampaignItem.id)).filter(
        BatchCampaignItem.campaign_id == campaign_id,
    ).scalar() or 0
    existing_by_sha = {
        row.sha256: row for row in db.query(BatchCampaignItem).filter(
            BatchCampaignItem.campaign_id == campaign_id,
            BatchCampaignItem.sha256.in_([item.sha256.lower() for item in body.items]),
        ).all()
    }
    incoming_codes = {
        (item.technical_code or "").strip().upper()
        for item in body.items if (item.technical_code or "").strip()
    }
    existing_by_code = {
        row.technical_code: row for row in db.query(BatchCampaignItem).filter(
            BatchCampaignItem.campaign_id == campaign_id,
            BatchCampaignItem.technical_code.in_(incoming_codes),
        ).all()
    } if incoming_codes else {}
    seen_hashes = set(existing_by_sha)
    seen_codes = set(existing_by_code)
    new_count = 0
    for item in body.items:
        digest = item.sha256.lower()
        code = (item.technical_code or "").strip().upper()
        if digest not in seen_hashes and (not code or code not in seen_codes):
            new_count += 1
            seen_hashes.add(digest)
            if code:
                seen_codes.add(code)
    if existing_count + new_count > ITEM_LIMIT:
        raise HTTPException(status_code=413, detail=f"Campaign limit is {ITEM_LIMIT} items.")
    results = []
    next_ordinal = int(existing_count)
    for incoming in body.items:
        digest = incoming.sha256.lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise HTTPException(status_code=400, detail=f"Invalid SHA-256 for {incoming.filename}.")
        code = (incoming.technical_code or "").strip().upper() or None
        sha_row = existing_by_sha.get(digest)
        code_row = existing_by_code.get(code) if code else None
        row = sha_row or code_row
        duplicate = row is not None
        duplicate_reason = "sha256" if sha_row is not None else (
            "technical_code" if code_row is not None else None
        )
        if row is None:
            next_ordinal += 1
            title = (incoming.title or "").strip() or None
            artist = (incoming.artist or "").strip() or None
            metadata_error = incoming.metadata_error
            if incoming.size_bytes > MAX_AUDIO_BYTES:
                metadata_error = "invalid_size"
            elif incoming.duration_seconds is not None and (
                incoming.duration_seconds <= 0
                or incoming.duration_seconds > MAX_AUDIO_DURATION
            ):
                metadata_error = "invalid_duration"
            if not title or not artist or not code:
                metadata_error = metadata_error or "missing_metadata"
            row = BatchCampaignItem(
                id=str(uuid.uuid4()),
                campaign_id=campaign.id,
                tenant_id=session.tenant_id,
                ordinal=next_ordinal,
                filename=incoming.filename,
                title=title,
                artist=artist,
                technical_code=code,
                size_bytes=incoming.size_bytes,
                duration_seconds=incoming.duration_seconds,
                sha256=digest,
                metadata_error=metadata_error,
                upload_state="registered",
                created_at=_now(),
                updated_at=_now(),
            )
            db.add(row)
            db.flush()
            existing_by_sha[digest] = row
            if code:
                existing_by_code[code] = row
        results.append({
            "client_id": incoming.client_id,
            "item_id": row.id,
            "state": row.upload_state,
            "duplicate": duplicate,
            "duplicate_reason": duplicate_reason,
        })
    campaign.expected_count = max(campaign.expected_count or 0, existing_count + new_count)
    campaign.updated_at = _now()
    db.commit()
    return {"items": results, "registered_count": existing_count + new_count}


def _campaign_object_key(item: BatchCampaignItem) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", item.filename).strip("._") or "audio.wav"
    return f"campaign-inputs/{item.tenant_id}/{item.campaign_id}/{item.id}/{safe}"


@router.post("/uploads/{item_id}/ticket")
def campaign_upload_ticket(
    item_id: str,
    x_batch_upload_token: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    session = _upload_session_or_401(db, x_batch_upload_token)
    item = db.query(BatchCampaignItem).filter(
        BatchCampaignItem.id == item_id,
        BatchCampaignItem.campaign_id == session.campaign_id,
        BatchCampaignItem.tenant_id == session.tenant_id,
    ).with_for_update().first()
    if item is None:
        raise HTTPException(status_code=404, detail="Campaign item not found.")
    if item.metadata_error in {"invalid_size", "invalid_duration"}:
        raise HTTPException(status_code=422, detail=item.metadata_error)
    if item.upload_state == "uploaded":
        return {"complete": True, "key": item.upload_key}
    key = item.upload_key or _campaign_object_key(item)
    content_type = "audio/wav" if item.filename.lower().endswith(".wav") else "audio/mpeg"
    item.upload_key = key
    item.upload_state = "uploading"
    item.upload_attempts = int(item.upload_attempts or 0) + 1
    item.upload_error = None
    use_multipart = item.size_bytes >= MULTIPART_THRESHOLD
    response: dict[str, Any] = {
        "complete": False,
        "use_multipart": use_multipart,
        "part_size": PART_SIZE,
        "key": key,
        "content_type": content_type,
    }
    if use_multipart:
        upload_id = item.multipart_upload_id
        uploaded_parts = None
        if upload_id:
            uploaded_parts = storage.multipart_list_parts(key, upload_id)
            if uploaded_parts is None:
                storage.multipart_abort(key, upload_id)
                upload_id = None
                item.multipart_upload_id = None
        if not upload_id:
            started = storage.multipart_init_object_key(key, content_type=content_type)
            if not started:
                raise HTTPException(status_code=503, detail="Could not start multipart upload.")
            upload_id = started["upload_id"]
            item.multipart_upload_id = upload_id
            uploaded_parts = []
        count = math.ceil(item.size_bytes / PART_SIZE)
        response["upload_id"] = upload_id
        response["uploaded_parts"] = uploaded_parts
        response["parts"] = [
            {
                "part_number": number,
                "url": storage.multipart_presign_part(
                    key, upload_id, number, expiry_seconds=3600,
                ),
            }
            for number in range(1, count + 1)
        ]
    else:
        signed = storage.presign_put_object_key(
            key, content_type=content_type, expiry_seconds=900,
        )
        if not signed:
            raise HTTPException(status_code=503, detail="Could not sign upload URL.")
        response["upload_url"] = signed["url"]
    db.commit()
    return response


@router.post("/uploads/{item_id}/complete")
def campaign_upload_complete(
    item_id: str,
    body: UploadComplete,
    x_batch_upload_token: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    session = _upload_session_or_401(db, x_batch_upload_token)
    item = db.query(BatchCampaignItem).filter(
        BatchCampaignItem.id == item_id,
        BatchCampaignItem.campaign_id == session.campaign_id,
        BatchCampaignItem.tenant_id == session.tenant_id,
    ).with_for_update().first()
    if item is None:
        raise HTTPException(status_code=404, detail="Campaign item not found.")
    if item.upload_state == "uploaded":
        return {"ok": True, "deduplicated": True}
    if not item.upload_key:
        raise HTTPException(status_code=409, detail="Upload ticket was not created.")
    if item.multipart_upload_id:
        normalized = []
        for part in body.parts:
            number = int(part.get("part_number") or part.get("PartNumber") or 0)
            etag = str(part.get("etag") or part.get("ETag") or "").strip('"')
            if number < 1 or not etag:
                raise HTTPException(status_code=400, detail="Invalid multipart completion payload.")
            normalized.append({"PartNumber": number, "ETag": etag})
        if not normalized:
            raise HTTPException(status_code=400, detail="Multipart completion requires parts.")
        storage.multipart_complete(item.upload_key, item.multipart_upload_id, normalized)
    real_size = storage.head_object_size(item.upload_key)
    if real_size is None or int(real_size) != int(item.size_bytes):
        item.upload_state = "error"
        item.upload_error = "uploaded_size_mismatch"
        db.commit()
        raise HTTPException(status_code=409, detail="Uploaded file size does not match manifest.")
    item.upload_state = "uploaded"
    item.multipart_upload_id = None
    item.upload_error = None
    item.uploaded_at = _now()
    item.updated_at = _now()
    db.commit()
    ensure_campaign_reconciler_scheduled()
    return {"ok": True, "size_bytes": real_size}


@router.post("/campaigns/{campaign_id}/next")
def claim_next_review(
    campaign_id: str,
    skip_job_id: str | None = Query(default=None, min_length=12, max_length=12),
    x_editor_session: str | None = Header(default=None),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_scope(current_user)
    campaign = _campaign_or_404(db, campaign_id, current_user)
    if campaign.status != "active":
        raise HTTPException(status_code=409, detail="Campaign is not reviewable.")
    session_id = (x_editor_session or "").strip()
    if not _SAFE_SESSION_RE.fullmatch(session_id):
        raise HTTPException(status_code=400, detail="A valid editor session is required.")
    now = _now()
    existing = db.query(Job, EditorDocument).join(
        EditorDocument, EditorDocument.job_id == Job.job_id,
    ).filter(
        Job.campaign_id == campaign.id,
        Job.tenant_id == campaign.tenant_id,
        Job.status.in_(("transcribed_pending", "transcribed")),
        EditorDocument.lock_user_id == current_user["id"],
        EditorDocument.lock_session_id == session_id,
        EditorDocument.lock_expires_at > now,
        *([Job.job_id != skip_job_id] if skip_job_id else []),
    ).first()
    if existing:
        job, _ = existing
        return {"job_id": job.job_id, "deduplicated": True}

    candidate_pairs = db.query(Job, BatchCampaignItem).join(
        BatchCampaignItem, BatchCampaignItem.id == Job.campaign_item_id,
    ).outerjoin(EditorDocument, EditorDocument.job_id == Job.job_id).filter(
        Job.campaign_id == campaign.id,
        Job.tenant_id == campaign.tenant_id,
        Job.status.in_(("transcribed_pending", "transcribed")),
        *([Job.job_id != skip_job_id] if skip_job_id else []),
        or_(
            EditorDocument.job_id.is_(None),
            EditorDocument.lock_expires_at.is_(None),
            EditorDocument.lock_expires_at <= now,
        ),
    ).all()
    verdicts = _latest_semaforo_verdicts(
        db, [job.job_id for job, _item in candidate_pairs],
    )
    candidate_pairs.sort(
        key=lambda pair: _delivery_rank(pair[1], verdicts.get(pair[0].job_id)),
    )
    if not candidate_pairs:
        return {"job_id": None, "empty": True}
    from editor import acquire_lock, get_or_create_document
    for job, item in candidate_pairs:
        document = get_or_create_document(
            db, job.job_id, job.tenant_id, job.segments_json or [],
        )
        lock = acquire_lock(db, document, current_user["id"], session_id=session_id)
        if lock.get("acquired"):
            db.commit()
            return {
                "job_id": job.job_id,
                "open_path": f"/review/{job.job_id}",
                "default_render_params": campaign.default_render_params or {},
                "render_overrides": item.render_overrides if item else {},
            }
    db.rollback()
    return {"job_id": None, "empty": True}


def _review_line_ids(segments: list[dict[str, Any]]) -> list[str]:
    line_ids: list[str] = []
    for segment in segments:
        value = str(segment.get("segment_id") or segment.get("id") or "").strip()
        if not value or value in line_ids:
            raise HTTPException(
                status_code=409,
                detail={"code": "review_line_identity_missing"},
            )
        line_ids.append(value)
    if not line_ids:
        raise HTTPException(status_code=409, detail={"code": "review_has_no_lines"})
    return line_ids


def require_prebackground_approval(job: Job) -> dict[str, Any]:
    """Fail closed unless the exact audio/editor revision was human-approved."""
    quality = dict(job.transcription_quality or {})
    hypothesis = quality.get("reference_hypothesis")
    from reference_hypothesis import validate_binding
    reference_ok, reference_reason = validate_binding(
        hypothesis,
        audio_sha256=str(job.input_audio_sha256 or ""),
        audio_revision=int(job.audio_revision or 0),
    )
    if not reference_ok:
        raise HTTPException(
            status_code=409,
            detail={"code": reference_reason, "stage": "lyrics_and_timing"},
        )
    approval = quality.get("pre_background_approval")
    if not isinstance(approval, dict):
        raise HTTPException(
            status_code=409,
            detail={"code": "lyrics_and_timing_approval_missing"},
        )
    from transcription_quality import segments_hash
    expected_hash = segments_hash(job.segments_json or [])
    if (
        int(approval.get("audio_revision") if approval.get("audio_revision") is not None else -1) != int(job.audio_revision or 0)
        or str(approval.get("audio_sha256") or "") != str(job.input_audio_sha256 or "")
        or int(approval.get("editor_revision") if approval.get("editor_revision") is not None else -1) != int(job.segments_revision or 0)
        or str(approval.get("segments_sha256") or "") != expected_hash
        or approval.get("lyrics_confirmed") is not True
        or approval.get("timings_confirmed") is not True
        or approval.get("heard_against_audio") is not True
    ):
        raise HTTPException(
            status_code=409,
            detail={"code": "lyrics_and_timing_approval_stale"},
        )
    return approval


@router.post("/campaigns/{campaign_id}/jobs/{job_id}/approve-lyrics")
def approve_campaign_lyrics(
    campaign_id: str,
    job_id: str,
    body: LyricsApprovalRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Bind one explicit song-level approval to the exact editor snapshot."""
    _require_scope(current_user)
    campaign = _campaign_or_404(db, campaign_id, current_user)
    if campaign.status != "active":
        raise HTTPException(status_code=409, detail="Campaign is not reviewable.")
    job = db.query(Job).filter(
        Job.job_id == job_id,
        Job.campaign_id == campaign.id,
        # `_campaign_or_404` already enforces tenant isolation for ordinary
        # reviewers and deliberately lets platform admins open a campaign
        # across tenants. Re-applying the actor's tenant here made that admin
        # access read-only by accident: editor/autosave worked, but approval
        # returned a misleading 404. Bind the job to the campaign tenant.
        Job.tenant_id == campaign.tenant_id,
    ).with_for_update().first()
    if job is None:
        raise HTTPException(status_code=404, detail="Campaign job not found.")
    if not (
        body.lyrics_confirmed
        and body.timings_confirmed
        and body.heard_against_audio
    ):
        raise HTTPException(
            status_code=422,
            detail={"code": "complete_human_review_required"},
        )
    quality = dict(job.transcription_quality or {})
    existing = quality.get("pre_background_approval") or {}
    if job.status == "lyrics_approved":
        require_prebackground_approval(job)
        if int(existing.get("editor_revision") if existing.get("editor_revision") is not None else -1) == body.editor_revision:
            return {
                "job_id": job_id, "status": job.status,
                "approved_version_id": existing.get("editor_version_id"),
                "deduplicated": True,
            }
    if job.status not in {"transcribed_pending", "transcribed"}:
        raise HTTPException(
            status_code=409,
            detail={"code": "job_not_awaiting_lyrics_review", "status": job.status},
        )
    require_prebackground_reference = quality.get("reference_hypothesis")
    from reference_hypothesis import validate_binding
    reference_ok, reference_reason = validate_binding(
        require_prebackground_reference,
        audio_sha256=str(job.input_audio_sha256 or ""),
        audio_revision=int(job.audio_revision or 0),
    )
    if not reference_ok:
        raise HTTPException(status_code=409, detail={"code": reference_reason})
    from editor import approve_document
    try:
        document, version = approve_document(
            db,
            job,
            current_user["id"],
            editor_revision=body.editor_revision,
            editor_version_id=body.editor_version_id,
        )
    except LookupError:
        raise HTTPException(status_code=409, detail="editor_version_not_found") from None
    except MachineSnapshotMissing:
        raise HTTPException(
            status_code=409,
            detail={"code": "machine_snapshot_missing"},
        ) from None
    except RuntimeError:
        raise HTTPException(status_code=409, detail="editor_revision_conflict") from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    expected_ids = _review_line_ids(list(document.current_segments or []))
    submitted_ids = [str(value) for value in body.confirmed_line_ids]
    if submitted_ids != expected_ids:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "review_lines_incomplete_or_stale",
                "expected_count": len(expected_ids),
                "confirmed_count": len(submitted_ids),
            },
        )
    from transcription_quality import segments_hash
    approval = {
        "schema": "batch-pre-background-approval-v1",
        "review_scope": body.review_scope,
        "audio_sha256": str(job.input_audio_sha256 or ""),
        "audio_revision": int(job.audio_revision or 0),
        "editor_revision": int(document.revision or 0),
        "editor_version_id": version.id,
        "segments_sha256": segments_hash(list(document.current_segments or [])),
        "confirmed_line_count": len(expected_ids),
        "lyrics_confirmed": True,
        "timings_confirmed": True,
        "heard_against_audio": True,
        "reviewer_user_id": current_user["id"],
        "approved_at": _now().isoformat(),
    }
    reference = dict(quality["reference_hypothesis"])
    reference["review_status"] = "human_line_review_approved"
    reference["reviewed_editor_revision"] = int(document.revision or 0)
    quality["reference_hypothesis"] = reference
    quality["pre_background_approval"] = approval
    job.transcription_quality = quality
    job.status = "lyrics_approved"
    job.current_step = "lyrics_and_timing_approved"
    job.progress = 100
    db.add(AuditLog(
        user_id=current_user["id"],
        action="batch.lyrics_and_timing_approved",
        detail={
            "campaign_id": campaign.id,
            "job_id": job_id,
            "audio_sha256": approval["audio_sha256"],
            "audio_revision": approval["audio_revision"],
            "editor_revision": approval["editor_revision"],
            "editor_version_id": approval["editor_version_id"],
            "segments_sha256": approval["segments_sha256"],
            "confirmed_line_count": approval["confirmed_line_count"],
        },
    ))
    db.add(ProductEvent(
        tenant_id=str(job.tenant_id), user_id=current_user["id"],
        job_id=job_id, name="batch_lyrics_and_timing_approved",
        occurred_at=_now(), properties={
            "campaign_id": campaign.id,
            "editor_revision": approval["editor_revision"],
            "confirmed_line_count": approval["confirmed_line_count"],
        },
    ))
    db.commit()
    return {
        "job_id": job_id, "status": "lyrics_approved",
        "approved_version_id": version.id, "deduplicated": False,
    }


def _queue_state(stage: str, job: Job | None, document: EditorDocument | None) -> str:
    if job is None:
        return "pending"
    now = _now()
    locked = bool(
        document and document.lock_user_id
        and _aware(document.lock_expires_at)
        and _aware(document.lock_expires_at) > now
    )
    if stage == "lyrics":
        if job.status in _ACTIVE_TRANSCRIPTION or job.status in {"awaiting_upload"}:
            return "processing"
        if job.status in {"transcribed_pending", "transcribed"}:
            return "reviewing" if locked else "ready"
        if job.status in {"lyrics_approved", "queued", "processing", "rendering", "pending_review", "done"}:
            return "approved"
    else:
        if job.status in _ACTIVE_RENDER or job.status == "lyrics_approved":
            return "processing"
        if job.status == "pending_review":
            return "reviewing" if locked else "ready"
        if job.status == "done":
            return "exported" if job.video_url or job.s3_keys else "approved"
    return "failed" if job.status in _FAILURE else "pending"


def _review_minutes_by_job(
    db: Session,
    job_ids: list[str],
    *,
    since: datetime | None = None,
) -> dict[str, float]:
    if not job_ids:
        return {}
    query = db.query(ProductEvent).filter(
        ProductEvent.name == "editor_activity_heartbeat",
        ProductEvent.job_id.in_(job_ids),
    )
    if since is not None:
        query = query.filter(ProductEvent.created_at >= since)
    events = query.order_by(ProductEvent.created_at.asc()).all()
    stamps: dict[tuple[str, int | None], list[datetime]] = {}
    for event in events:
        when = _aware(event.occurred_at or event.created_at)
        if when is not None:
            stamps.setdefault((str(event.job_id), event.user_id), []).append(when)
    seconds_by_job: dict[str, float] = {}
    for (job_id, _user_id), values in stamps.items():
        seconds = sum(
            gap for gap in (
                (right - left).total_seconds()
                for left, right in zip(values, values[1:])
            ) if 0 < gap <= 25.0
        )
        seconds_by_job[job_id] = seconds_by_job.get(job_id, 0.0) + (
            seconds if seconds > 0 else 15.0
        )
    return {
        job_id: round(seconds / 60.0, 2)
        for job_id, seconds in seconds_by_job.items()
    }


def _review_minutes_today(db: Session, job_ids: list[str]) -> dict[str, Any]:
    today = _now().replace(hour=0, minute=0, second=0, microsecond=0)
    minutes_by_job = _review_minutes_by_job(db, job_ids, since=today)
    minutes = list(minutes_by_job.values())
    return {
        "average": round(sum(minutes) / len(minutes), 2) if minutes else None,
        "total": round(sum(minutes), 2),
        "songs": len(minutes),
        "source": "editor_activity_heartbeat_v1",
    }


_REFERENCE_LINK_KINDS = frozenset({
    "fan_site", "aggregator", "official_artist_site", "official_channel",
})


def _review_reference_links(overrides: dict[str, Any]) -> list[dict[str, str]]:
    """Expose inert reviewer pointers; never retrieve or process their text."""
    rows = overrides.get("review_reference_links") or []
    result: list[dict[str, str]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        kind = str(row.get("kind") or "").strip().lower()
        url = str(row.get("url") or "").strip()
        parsed = urlparse(url)
        if kind in _REFERENCE_LINK_KINDS and parsed.scheme == "https" and parsed.netloc:
            result.append({"kind": kind, "url": url})
    return result[:10]


@router.get("/campaigns/{campaign_id}/review-queue")
def review_queue(
    campaign_id: str,
    stage: str = Query(default="lyrics", pattern="^(lyrics|final)$"),
    order: str = Query(default="delivery", pattern="^(delivery|learning)$"),
    state: str | None = None,
    version: str | None = Query(default=None, pattern="^(studio|live)$"),
    background_mode: str | None = None,
    artist: str | None = None,
    audit_preapproved: bool = False,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Operational queue for pre-background and post-render review."""
    _require_scope(current_user)
    campaign = _campaign_or_404(db, campaign_id, current_user)
    pairs = _campaign_rows(db, campaign.id)
    job_ids = [job.job_id for _, job in pairs if job is not None]
    documents = {
        row.job_id: row for row in db.query(EditorDocument).filter(
            EditorDocument.job_id.in_(job_ids)
        ).all()
    } if job_ids else {}
    reviewer_ids = {
        int(row.lock_user_id) for row in documents.values()
        if row.lock_user_id is not None
    }
    reviewers = {
        row.id: (row.full_name or row.username or row.email or f"user-{row.id}")
        for row in db.query(User).filter(User.id.in_(reviewer_ids)).all()
    } if reviewer_ids else {}
    verdicts = _latest_semaforo_verdicts(db, job_ids)
    queue_config = dict((campaign.default_render_params or {}).get("review_queue") or {})
    calibration_target = max(
        50, min(80, int(_number(queue_config.get("calibration_target"), 50))),
    )
    confidence_gate_passed = bool(queue_config.get("confidence_gate_passed"))
    active_minutes = _review_minutes_by_job(db, job_ids)
    rows: list[dict[str, Any]] = []
    for item, job in pairs:
        document = documents.get(job.job_id) if job else None
        queue_state = _queue_state(stage, job, document)
        title_version = "live" if (
            "live" in str(item.title or "").lower()
            or "en vivo" in str(item.title or "").lower()
            or "live" in str(item.filename or "").lower()
        ) else "studio"
        overrides = dict(item.render_overrides or {})
        bg_mode = str(
            overrides.get("background_mode")
            or (campaign.default_render_params or {}).get("background_mode")
            or "generated"
        )
        if state and queue_state != state:
            continue
        if version and title_version != version:
            continue
        if background_mode and bg_mode != background_mode:
            continue
        if artist and artist.lower() not in str(item.artist or "").lower():
            continue
        verdict = verdicts.get(job.job_id, {}) if job else {}
        color = str(verdict.get("color") or "red").lower()
        color_rank = {"green": 0, "yellow": 1, "red": 2}.get(color, 2)
        quality = dict(job.transcription_quality or {}) if job else {}
        reference = dict(quality.get("reference_hypothesis") or {})
        reference_available = False
        if job and reference:
            from reference_hypothesis import validate_binding
            reference_available, _reference_reason = validate_binding(
                reference,
                audio_sha256=str(job.input_audio_sha256 or ""),
                audio_revision=int(job.audio_revision or 0),
            )
            reference_available = bool(
                reference_available
                and reference.get("availability") != "unavailable"
                and reference.get("review_status") != "manual_full_review_required"
                and int(reference.get("line_count") or 0) > 0
            )
        metadata_review_required = bool(
            item.metadata_error
            and item.metadata_error not in {"invalid_size", "invalid_duration"}
        )
        segments = list(job.segments_json or []) if job else []
        lyric_line_count = sum(
            1 for segment in segments
            if isinstance(segment, dict) and str(segment.get("text") or "").strip()
        )
        empty_transcription = bool(job and lyric_line_count == 0)
        manual_reasons = []
        if not reference_available:
            manual_reasons.append("missing_reference")
        if empty_transcription:
            manual_reasons.append("empty_transcription")
        if metadata_review_required:
            manual_reasons.append("metadata_review")
        if quality.get("manual_full_review_required") and not manual_reasons:
            manual_reasons.append("quality_manual_review")
        manual_full_review = bool(
            quality.get("manual_full_review_required")
            or manual_reasons
        )
        doubt_count = len([
            window for window in (quality.get("unsafe_windows") or [])
            if isinstance(window, dict)
        ])
        rows.append({
            "item_id": item.id,
            "job_id": job.job_id if job else None,
            "ordinal": item.ordinal,
            "artist": item.artist or "",
            "title": item.title or item.filename,
            "version": title_version,
            "background_mode": bg_mode if stage == "final" else None,
            "metadata_review_required": metadata_review_required,
            "lyric_line_count": lyric_line_count,
            "doubt_count": doubt_count,
            "review_group": "manual" if manual_full_review else "standard",
            "manual_reasons": manual_reasons,
            "duration_seconds": item.duration_seconds,
            "active_minutes": active_minutes.get(job.job_id, 0.0) if job else 0.0,
            "state": queue_state,
            "reviewer_user_id": document.lock_user_id if document else None,
            "reviewer_name": (
                reviewers.get(document.lock_user_id) if document else None
            ),
            "priority": "",
            "semaforo": verdict.get("color") if confidence_gate_passed else None,
            "semaforo_hidden": not confidence_gate_passed,
            "_semaforo_rank": color_rank,
            "_delivery_rank": _number(verdict.get("rank_key"), 9_999.0),
            "disagreement": _number(
                (verdict.get("inputs") or {}).get("disagreement")
                if isinstance(verdict.get("inputs"), dict)
                else verdict.get("disagreement") or verdict.get("score"),
            ),
            "reference": {
                "available": reference_available,
                "provider": (reference.get("source") or {}).get("provider"),
                "source_kind": (reference.get("source") or {}).get("kind"),
                "status": reference.get("review_status"),
                "line_count": reference.get("line_count"),
                "manual_full_review_required": manual_full_review,
                "external_links": _review_reference_links(overrides),
            },
            "open_path": (
                f"/review/{job.job_id}" if stage == "lyrics" and job
                else f"/videos/{job.job_id}" if job else None
            ),
        })
    if audit_preapproved:
        rows = [
            row for row in rows
            if confidence_gate_passed
            and str(row.get("semaforo") or "").lower() == "green"
            and row["state"] in {"approved", "exported"}
        ]
    counter_rows = list(rows)
    if order == "learning":
        rows.sort(key=lambda row: (-row["disagreement"], row["ordinal"]))
        rows = rows[:max(1, math.ceil(len(rows) * 0.20))]
    else:
        rows.sort(key=lambda row: (
            1 if row["reference"]["manual_full_review_required"] else 0,
            1 if row["version"] == "live" else 0,
            row["_semaforo_rank"] if confidence_gate_passed else 0,
            row["_delivery_rank"] if confidence_gate_passed else row["doubt_count"],
            row["ordinal"],
        ))
    # One priority column only. During blind calibration it exposes merely
    # the queue position (the ordering itself is required) and never a color
    # or three-bucket proxy. After the confidence gate the same column shows
    # the actual semaforo label.
    for position, row in enumerate(rows, start=1):
        row["priority"] = (
            str(row.get("semaforo") or "red")
            if confidence_gate_passed else str(position)
        )
    counters = {key: 0 for key in (
        "pending", "processing", "ready", "reviewing", "approved",
        "approved_today", "exported", "failed",
    )}
    for row in counter_rows:
        counters[row["state"]] = counters.get(row["state"], 0) + 1
    today = _now().replace(hour=0, minute=0, second=0, microsecond=0)
    if stage == "lyrics":
        # Count only campaign jobs from the already materialized rows to
        # avoid JSON-path dialect drift between SQLite and PostgreSQL.
        approved_today_ids = {
            str((log.detail or {}).get("job_id") or "")
            for log in db.query(AuditLog).filter(
                AuditLog.action == "batch.lyrics_and_timing_approved",
                AuditLog.created_at >= today,
            ).all()
        }
        counters["approved_today"] = len(approved_today_ids.intersection(job_ids))
    else:
        counters["approved_today"] = sum(
            bool(job and _aware(job.approved_at) and _aware(job.approved_at) >= today)
            for _, job in pairs
        )
    total = len(rows)
    start = (page - 1) * limit
    background_split = ({
        "fixed": sum(row["background_mode"] in {"fixed", "as_is", "library"} for row in counter_rows),
        "generated": sum(row["background_mode"] not in {"fixed", "as_is", "library"} for row in counter_rows),
    } if stage == "final" else None)
    return {
        "campaign_id": campaign.id,
        "stage": stage,
        "order": order,
        "items": [
            {key: value for key, value in row.items() if not key.startswith("_")}
            for row in rows[start:start + limit]
        ],
        "total": total,
        "page": page,
        "pages": max(1, math.ceil(total / limit)),
        "counters": counters,
        "background_split": background_split,
        "review_minutes_today": _review_minutes_today(db, job_ids),
        "confidence": {
            "gate_passed": confidence_gate_passed,
            "calibration_target": calibration_target,
            "colors_visible": confidence_gate_passed,
            "preapproved_audit_available": confidence_gate_passed,
        },
    }


@router.post("/campaigns/{campaign_id}/review-queue/next")
def claim_next_stage_review(
    campaign_id: str,
    stage: str = Query(default="lyrics", pattern="^(lyrics|final)$"),
    skip_job_id: str | None = Query(default=None, min_length=12, max_length=12),
    x_editor_session: str | None = Header(default=None),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if stage == "lyrics":
        return claim_next_review(
            campaign_id,
            skip_job_id,
            x_editor_session,
            current_user,
            db,
        )
    _require_scope(current_user)
    campaign = _campaign_or_404(db, campaign_id, current_user)
    if campaign.status != "active":
        raise HTTPException(status_code=409, detail="Campaign is not reviewable.")
    session_id = (x_editor_session or "").strip()
    if not _SAFE_SESSION_RE.fullmatch(session_id):
        raise HTTPException(status_code=400, detail="A valid editor session is required.")
    now = _now()
    candidate_pairs = db.query(Job, BatchCampaignItem).join(
        BatchCampaignItem, BatchCampaignItem.id == Job.campaign_item_id,
    ).outerjoin(EditorDocument, EditorDocument.job_id == Job.job_id).filter(
        Job.campaign_id == campaign.id,
        Job.tenant_id == current_user["tenant_id"],
        Job.status == "pending_review",
        or_(
            EditorDocument.job_id.is_(None),
            EditorDocument.lock_expires_at.is_(None),
            EditorDocument.lock_expires_at <= now,
        ),
    ).all()
    verdicts = _latest_semaforo_verdicts(
        db, [job.job_id for job, _item in candidate_pairs],
    )
    candidate_pairs.sort(
        key=lambda pair: _delivery_rank(pair[1], verdicts.get(pair[0].job_id)),
    )
    from editor import acquire_lock, get_or_create_document
    for job, _item in candidate_pairs:
        document = get_or_create_document(db, job.job_id, job.tenant_id, job.segments_json or [])
        if acquire_lock(db, document, current_user["id"], session_id=session_id).get("acquired"):
            db.commit()
            return {"job_id": job.job_id, "open_path": f"/videos/{job.job_id}"}
    db.rollback()
    return {"job_id": None, "empty": True}


def _batch_transcription_kwargs(
    campaign: BatchCampaign,
    item: BatchCampaignItem,
    *,
    pipeline_stage: str = "full",
) -> dict[str, Any]:
    """Build one audio-first transcription request for every campaign path."""
    title = item.title or ""
    lowered_title = title.lower()
    return {
        # Empty means provider auto-LID.  The worker confirms the language on
        # the vocal stem and deliberately keeps mixed-language input unforced.
        "language": "",
        "artist": item.artist or "",
        "title": title,
        "filename": item.filename,
        "tenant_id": campaign.tenant_id,
        "live": "live" in lowered_title or "en vivo" in lowered_title,
        "anchor_lyrics": "",
        "reference_required": True,
        "workload_class": "batch",
        "pipeline_stage": pipeline_stage,
        # The full-audio Gemini hypothesis is independent evidence.  In the
        # full stage it runs alongside blind ASR and is joined before
        # attestation/reconciliation.  No catalogue/web lyric text is used.
        "parallel_audio_reference": True,
    }


def _stage1_pipeline_settings(campaign: BatchCampaign) -> dict[str, Any]:
    raw = dict((campaign.default_render_params or {}).get("stage1_pipeline") or {})
    try:
        requested_ready_limit = int(raw.get("lyrics_ready_limit", LYRICS_READY_LIMIT))
    except (TypeError, ValueError):
        requested_ready_limit = LYRICS_READY_LIMIT
    try:
        requested_promotion_limit = int(raw.get("promotion_limit", ITEM_LIMIT))
    except (TypeError, ValueError):
        requested_promotion_limit = ITEM_LIMIT
    return {
        "prewarm_separation": bool(raw.get("prewarm_separation", False)),
        "lyrics_ready_limit": max(
            1, min(ITEM_LIMIT, requested_ready_limit),
        ),
        # A campaign can prove a real canary against its first N delivery-order
        # rows, then raise this limit without duplicating uploads or jobs.
        "promotion_limit": max(
            1, min(ITEM_LIMIT, requested_promotion_limit),
        ),
    }


def _processable_items_query(db: Session, campaign: BatchCampaign):
    """Uploaded audio eligible for stage 1, including manual metadata rows.

    Missing/conflicting metadata must make the row red for human review, not
    suppress transcription of an otherwise valid official audio asset.
    """
    return db.query(BatchCampaignItem).filter(
        BatchCampaignItem.campaign_id == campaign.id,
        BatchCampaignItem.upload_state == "uploaded",
        or_(
            BatchCampaignItem.metadata_error.is_(None),
            ~BatchCampaignItem.metadata_error.in_((
                "invalid_size", "invalid_duration", "promotion_failed",
            )),
        ),
    )


def _create_stage_event(
    db: Session,
    campaign: BatchCampaign,
    item: BatchCampaignItem,
    *,
    pipeline_stage: str,
) -> str:
    from transactional_outbox import create_transcription_outbox_event

    job_id = create_job(
        db,
        artist=item.artist or "Unknown",
        song_title=item.title or "",
        style="auto",
        filename=item.filename,
        user_id=campaign.created_by,
        tenant_id=campaign.tenant_id,
        delivery_profile="youtube",
        initial_status="awaiting_upload",
        input_r2_key=item.upload_key,
        workload_class="batch",
        campaign_id=campaign.id,
        campaign_item_id=item.id,
        commit=False,
    )
    job = db.query(Job).filter(Job.job_id == job_id).one()
    job.status = (
        "separation_queued" if pipeline_stage == "separation"
        else "transcribing_queued"
    )
    job.current_step = (
        "transcribe.separation_queued" if pipeline_stage == "separation"
        else "transcribe.prepare"
    )
    job.progress = 1
    job.last_progress_at = _now()
    audio_path = os.path.join(
        os.path.dirname(__file__), "..", "outputs", job_id, item.filename,
    )
    event = create_transcription_outbox_event(
        db,
        job=job,
        audio_path=audio_path,
        transcription_kwargs=_batch_transcription_kwargs(
            campaign, item, pipeline_stage=pipeline_stage,
        ),
    )
    return event.id


def _queue_full_stage_for_separated(
    db: Session,
    campaign: BatchCampaign,
    *,
    room: int,
) -> list[str]:
    from transactional_outbox import create_transcription_outbox_event

    pairs = db.query(Job, BatchCampaignItem).join(
        BatchCampaignItem, BatchCampaignItem.id == Job.campaign_item_id,
    ).filter(
        Job.campaign_id == campaign.id,
        Job.status == "separation_ready",
    ).order_by(BatchCampaignItem.ordinal.asc()).with_for_update(
        skip_locked=True,
    ).limit(room).all()
    event_ids: list[str] = []
    for job, item in pairs:
        try:
            event_id = None
            with db.begin_nested():
                audio_path = os.path.join(
                    os.path.dirname(__file__), "..", "outputs", job.job_id,
                    item.filename,
                )
                event = create_transcription_outbox_event(
                    db,
                    job=job,
                    audio_path=audio_path,
                    transcription_kwargs=_batch_transcription_kwargs(
                        campaign, item, pipeline_stage="full",
                    ),
                )
                job.status = "transcribing_queued"
                job.current_step = "transcribe.prepare"
                job.progress = max(21, int(job.progress or 0))
                job.last_progress_at = _now()
                event_id = event.id
            if event_id:
                event_ids.append(event_id)
        except Exception as exc:
            job.status = "transcription_failed"
            job.current_step = "error"
            job.error = "stage1_full_promotion_failed"
            job.error_category = type(exc).__name__[:100]
    if pairs:
        campaign.updated_at = _now()
        db.commit()
    return event_ids


def _promote_campaign(db: Session, campaign: BatchCampaign) -> list[str]:
    stage1_settings = _stage1_pipeline_settings(campaign)
    active_trans = db.query(func.count(Job.id)).filter(
        Job.tenant_id == campaign.tenant_id,
        Job.workload_class == "batch",
        Job.status.in_(_ACTIVE_TRANSCRIPTION),
    ).scalar() or 0
    ready = db.query(func.count(Job.id)).filter(
        Job.tenant_id == campaign.tenant_id,
        Job.workload_class == "batch",
        Job.status.in_(("transcribed_pending", "transcribed", "lyrics_approved")),
    ).scalar() or 0
    transcription_room = min(
        max(0, TRANSCRIPTION_WINDOW - active_trans),
        # Reserve room for every active transcription to finish. Without
        # this, 30 active jobs could complete on top of 40 ready lyrics and
        # overshoot the configured review buffer.
        max(0, stage1_settings["lyrics_ready_limit"] - ready - active_trans),
    )
    linked = db.query(Job.campaign_item_id).filter(Job.campaign_id == campaign.id)
    processable = _processable_items_query(db, campaign).filter(
        BatchCampaignItem.ordinal <= stage1_settings["promotion_limit"],
    )

    if stage1_settings["prewarm_separation"]:
        # Phase A is a durable barrier: enqueue/cache every stem before Phase B
        # releases any full transcription. Failed separations are terminal red
        # rows but do not prevent the remaining songs from crossing the barrier.
        active_separation = db.query(func.count(Job.id)).filter(
            Job.campaign_id == campaign.id,
            Job.status.in_(_ACTIVE_SEPARATION),
        ).scalar() or 0
        separation_room = max(0, SEPARATION_WINDOW - active_separation)
        unlinked_count = processable.filter(~BatchCampaignItem.id.in_(linked)).count()
        if unlinked_count and separation_room:
            items = processable.filter(
                ~BatchCampaignItem.id.in_(linked),
            ).order_by(BatchCampaignItem.ordinal.asc()).with_for_update(
                skip_locked=True,
            ).limit(separation_room).all()
            event_ids: list[str] = []
            for item in items:
                try:
                    event_id = None
                    with db.begin_nested():
                        event_id = _create_stage_event(
                            db, campaign, item, pipeline_stage="separation",
                        )
                    if event_id:
                        event_ids.append(event_id)
                except Exception as exc:
                    item.metadata_error = "promotion_failed"
                    item.upload_error = type(exc).__name__[:100]
            if items:
                campaign.updated_at = _now()
                db.commit()
            return event_ids
        if unlinked_count or active_separation:
            return []
        if transcription_room <= 0:
            return []
        return _queue_full_stage_for_separated(
            db, campaign, room=transcription_room,
        )

    if transcription_room <= 0:
        return []
    items = processable.filter(
        ~BatchCampaignItem.id.in_(linked),
    ).order_by(BatchCampaignItem.ordinal.asc()).with_for_update(
        skip_locked=True,
    ).limit(transcription_room).all()
    event_ids: list[str] = []
    for item in items:
        try:
            event_id = None
            with db.begin_nested():
                event_id = _create_stage_event(
                    db, campaign, item, pipeline_stage="full",
                )
            if event_id:
                event_ids.append(event_id)
        except Exception as exc:
            # One corrupt/conflicting row is visible as red and cannot abort
            # creation or dispatch for the rest of the wave.
            item.metadata_error = "promotion_failed"
            item.upload_error = type(exc).__name__[:100]
    if items:
        campaign.updated_at = _now()
        db.commit()
    return event_ids


def reconcile_batch_campaigns() -> dict[str, int]:
    """RQ entry point: fill bounded transcription buffers and reschedule."""
    promoted = 0
    dispatched = 0
    event_ids: list[str] = []
    if not feature_enabled():
        return {"promoted": 0, "dispatched": 0}
    db = SessionLocal()
    try:
        campaigns = db.query(BatchCampaign).filter(
            BatchCampaign.status == "active",
        ).order_by(BatchCampaign.created_at.asc()).all()
        for campaign in campaigns:
            event_ids.extend(_promote_campaign(db, campaign))
            rows = _campaign_rows(db, campaign.id)
            manifest_complete = bool(rows) and (
                not campaign.expected_count or len(rows) >= campaign.expected_count
            )
            if manifest_complete and all(
                job is not None and job.status == "done" for _, job in rows
            ):
                campaign.status = "completed"
                campaign.completed_at = _now()
                campaign.updated_at = _now()
                db.commit()
        promoted = len(event_ids)
    finally:
        db.close()
    from transactional_outbox import dispatch_outbox_event
    for event_id in event_ids:
        if dispatch_outbox_event(event_id).get("status") == "dispatched":
            dispatched += 1
    return {"promoted": promoted, "dispatched": dispatched}


def enforce_render_capacity(db: Session, job: Job) -> None:
    """Keep campaign rendering bounded without consuming interactive quota."""
    if job.workload_class != "batch" or not job.campaign_id:
        return
    require_prebackground_approval(job)
    campaign = db.query(BatchCampaign).filter(
        BatchCampaign.id == job.campaign_id,
    ).with_for_update().first()
    if campaign is None or campaign.status != "active":
        raise HTTPException(status_code=409, detail="Campaign is not active.")
    final_review = db.query(func.count(Job.id)).filter(
        Job.tenant_id == campaign.tenant_id,
        Job.workload_class == "batch",
        Job.status == "pending_review",
    ).scalar() or 0
    if final_review >= FINAL_REVIEW_LIMIT:
        raise HTTPException(
            status_code=429,
            detail={"code": "batch_final_review_full", "limit": FINAL_REVIEW_LIMIT},
        )
    active_render = db.query(func.count(Job.id)).filter(
        Job.tenant_id == campaign.tenant_id,
        Job.workload_class == "batch",
        Job.status.in_(_ACTIVE_RENDER),
        Job.job_id != job.job_id,
    ).scalar() or 0
    if active_render >= RENDER_WINDOW:
        raise HTTPException(
            status_code=429,
            detail={"code": "batch_render_window_full", "limit": RENDER_WINDOW},
        )


def context_for_job(db: Session, job: Job) -> dict[str, Any] | None:
    if job.workload_class != "batch" or not job.campaign_id:
        return None
    campaign = db.query(BatchCampaign).filter(
        BatchCampaign.id == job.campaign_id,
    ).first()
    item = db.query(BatchCampaignItem).filter(
        BatchCampaignItem.id == job.campaign_item_id,
    ).first() if job.campaign_item_id else None
    return {
        "campaign_id": job.campaign_id,
        "campaign_item_id": job.campaign_item_id,
        "campaign_status": campaign.status if campaign else None,
        "default_render_params": campaign.default_render_params if campaign else {},
        "render_overrides": item.render_overrides if item else {},
        "review_reference_links": _review_reference_links(
            dict(item.render_overrides or {}) if item else {},
        ),
    }


@router.post(
    "/campaigns/{campaign_id}/items/{item_id}/retry",
    response_model=RetryItemResponse,
)
def retry_campaign_item(
    campaign_id: str,
    item_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_scope(current_user)
    campaign = _campaign_or_404(db, campaign_id, current_user)
    _require_manager(campaign, current_user)
    if campaign.status != "active":
        raise HTTPException(status_code=409, detail="Campaign is not active.")
    item = db.query(BatchCampaignItem).filter(
        BatchCampaignItem.id == item_id,
        BatchCampaignItem.campaign_id == campaign.id,
    ).with_for_update().first()
    if item is None:
        raise HTTPException(status_code=404, detail="Campaign item not found.")
    job = db.query(Job).filter(Job.campaign_item_id == item.id).with_for_update().first()
    if job is None:
        if item.upload_state != "error":
            raise HTTPException(status_code=409, detail="Item has no retryable failure.")
        item.upload_state = "registered"
        item.upload_error = None
        item.multipart_upload_id = None
        item.updated_at = _now()
        db.commit()
        return RetryItemResponse(status="registered")
    if job.status == "transcription_failed":
        from transactional_outbox import create_transcription_outbox_event
        job.status = "transcribing_queued"
        job.current_step = "transcribe.prepare"
        job.progress = 1
        job.error = None
        job.last_progress_at = _now()
        audio_path = os.path.join(
            os.path.dirname(__file__), "..", "outputs", job.job_id, item.filename,
        )
        event = create_transcription_outbox_event(
            db,
            job=job,
            audio_path=audio_path,
            transcription_kwargs=_batch_transcription_kwargs(campaign, item),
        )
        db.commit()
        from transactional_outbox import dispatch_outbox_event
        dispatch_outbox_event(event.id)
        return RetryItemResponse(job_id=job.job_id, status="transcribing_queued")
    if job.status in _FAILURE:
        # Rendering retries return to the approved-lyrics gate. This keeps
        # all campaign retries on the batch queue and prevents an automatic
        # background charge after a failure.
        try:
            require_prebackground_approval(job)
            job.status = "lyrics_approved"
            job.current_step = "lyrics_and_timing_approved"
        except HTTPException:
            job.status = "transcribed_pending"
            job.current_step = "editing"
        job.progress = 100
        job.error = None
        job.last_progress_at = _now()
        db.commit()
        return RetryItemResponse(job_id=job.job_id, status=job.status)
    raise HTTPException(status_code=409, detail=f"Job in {job.status!r} is not retryable.")


def run_campaign_reconciler() -> dict[str, int]:
    try:
        return reconcile_batch_campaigns()
    finally:
        ensure_campaign_reconciler_scheduled()


def ensure_campaign_reconciler_scheduled() -> bool:
    if not feature_enabled():
        return False
    try:
        from queue_jobs import _init_redis, rq_payload_metadata
        from rq import Queue
        redis, _, _ = _init_redis()
        if redis is None:
            return False
        bucket = int(time.time() // max(5, RECONCILE_SECONDS))
        queue = Queue("campaign_control", connection=redis)
        queue.enqueue_in(
            timedelta(seconds=RECONCILE_SECONDS),
            run_campaign_reconciler,
            job_id=f"campaign-reconcile:{bucket}",
            job_timeout=120,
            result_ttl=60,
            failure_ttl=3600,
            meta=rq_payload_metadata("campaign_control"),
        )
        return True
    except Exception:
        return False
