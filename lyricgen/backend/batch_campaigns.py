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
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from auth import get_current_user
from database import (
    BatchCampaign,
    BatchCampaignItem,
    BatchUploadSession,
    EditorDocument,
    Job,
    SessionLocal,
    get_db,
)
from jobs import create_job
import storage


router = APIRouter(prefix="/batch", tags=["batch-campaigns"])

CAMPAIGN_STATUSES = frozenset({"active", "paused", "completed", "cancelled"})
ITEM_LIMIT = int(os.environ.get("BATCH_CAMPAIGN_ITEM_LIMIT", "1000"))
TRANSCRIPTION_WINDOW = int(os.environ.get("BATCH_TRANSCRIPTION_WINDOW", "30"))
LYRICS_READY_LIMIT = int(os.environ.get("BATCH_LYRICS_READY_LIMIT", "50"))
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
_ACTIVE_RENDER = frozenset({"queued", "processing", "editing", "background_generating", "rendering"})
_FAILURE = frozenset({"error", "transcription_failed", "validation_failed", "rejected"})
_SAFE_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


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
    campaign = db.query(BatchCampaign).filter(
        BatchCampaign.id == campaign_id,
        BatchCampaign.tenant_id == user["tenant_id"],
    ).first()
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found.")
    return campaign


def _require_manager(campaign: BatchCampaign, user: dict) -> None:
    if user.get("role") != "admin" and campaign.created_by != user.get("id"):
        raise HTTPException(status_code=403, detail="Only the campaign owner can change it.")


def _hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _phase(upload_state: str, job_status: str | None, metadata_error: str | None = None) -> str:
    if job_status in _ACTIVE_TRANSCRIPTION:
        return "transcribing"
    if job_status in {"transcribed_pending", "transcribed"}:
        return "lyrics_ready"
    if job_status in _ACTIVE_RENDER:
        return "rendering"
    if job_status == "pending_review":
        return "final_review"
    if job_status == "done":
        return "done"
    if job_status in _FAILURE or upload_state == "error" or metadata_error in {"invalid_size", "invalid_duration"}:
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
            "lyrics_ready", "rendering", "final_review", "done", "failed",
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
    rows = db.query(BatchCampaign).filter(
        BatchCampaign.tenant_id == current_user["tenant_id"],
    ).order_by(BatchCampaign.created_at.desc()).limit(100).all()
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
        Job.tenant_id == current_user["tenant_id"],
        Job.status.in_(("transcribed_pending", "transcribed")),
        EditorDocument.lock_user_id == current_user["id"],
        EditorDocument.lock_session_id == session_id,
        EditorDocument.lock_expires_at > now,
    ).first()
    if existing:
        job, _ = existing
        return {"job_id": job.job_id, "deduplicated": True}

    candidates = db.query(Job).join(
        BatchCampaignItem, BatchCampaignItem.id == Job.campaign_item_id,
    ).outerjoin(EditorDocument, EditorDocument.job_id == Job.job_id).filter(
        Job.campaign_id == campaign.id,
        Job.tenant_id == current_user["tenant_id"],
        Job.status.in_(("transcribed_pending", "transcribed")),
        or_(
            EditorDocument.job_id.is_(None),
            EditorDocument.lock_expires_at.is_(None),
            EditorDocument.lock_expires_at <= now,
        ),
    # PostgreSQL rejects a blanket FOR UPDATE when an OUTER JOIN is present
    # because the nullable editor_documents side cannot be locked. Lock only
    # the jobs that are being claimed; the editor lock is acquired separately
    # below under its own row lock.
    ).order_by(BatchCampaignItem.ordinal.asc()).with_for_update(
        of=Job, skip_locked=True,
    ).limit(10).all()
    if not candidates:
        return {"job_id": None, "empty": True}
    from editor import acquire_lock, get_or_create_document
    for job in candidates:
        document = get_or_create_document(
            db, job.job_id, job.tenant_id, job.segments_json or [],
        )
        lock = acquire_lock(db, document, current_user["id"], session_id=session_id)
        if lock.get("acquired"):
            db.commit()
            item = db.query(BatchCampaignItem).filter(
                BatchCampaignItem.id == job.campaign_item_id,
            ).first()
            return {
                "job_id": job.job_id,
                "default_render_params": campaign.default_render_params or {},
                "render_overrides": item.render_overrides if item else {},
            }
    db.rollback()
    return {"job_id": None, "empty": True}


def _batch_transcription_kwargs(
    campaign: BatchCampaign,
    item: BatchCampaignItem,
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
        "workload_class": "batch",
    }


def _promote_campaign(db: Session, campaign: BatchCampaign) -> list[str]:
    active_trans = db.query(func.count(Job.id)).filter(
        Job.tenant_id == campaign.tenant_id,
        Job.workload_class == "batch",
        Job.status.in_(_ACTIVE_TRANSCRIPTION),
    ).scalar() or 0
    ready = db.query(func.count(Job.id)).filter(
        Job.tenant_id == campaign.tenant_id,
        Job.workload_class == "batch",
        Job.status.in_(("transcribed_pending", "transcribed")),
    ).scalar() or 0
    room = min(
        max(0, TRANSCRIPTION_WINDOW - active_trans),
        # Reserve room for every active transcription to finish. Without
        # this, 30 active jobs could complete on top of 40 ready lyrics and
        # overshoot the promised 50-song review buffer.
        max(0, LYRICS_READY_LIMIT - ready - active_trans),
    )
    if room <= 0:
        return []
    linked = db.query(Job.campaign_item_id).filter(Job.campaign_id == campaign.id)
    items = db.query(BatchCampaignItem).filter(
        BatchCampaignItem.campaign_id == campaign.id,
        BatchCampaignItem.upload_state == "uploaded",
        BatchCampaignItem.metadata_error.is_(None),
        ~BatchCampaignItem.id.in_(linked),
    ).order_by(BatchCampaignItem.ordinal.asc()).with_for_update(skip_locked=True).limit(room).all()
    event_ids: list[str] = []
    from transactional_outbox import create_transcription_outbox_event
    for item in items:
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
        job.status = "transcribing_queued"
        job.current_step = "transcribe.prepare"
        job.progress = 1
        job.last_progress_at = _now()
        audio_path = os.path.join(
            os.path.dirname(__file__), "..", "outputs", job_id, item.filename,
        )
        event = create_transcription_outbox_event(
            db,
            job=job,
            audio_path=audio_path,
            transcription_kwargs=_batch_transcription_kwargs(campaign, item),
        )
        event_ids.append(event.id)
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
        job.status = "transcribed_pending"
        job.current_step = "editing"
        job.progress = 100
        job.error = None
        job.last_progress_at = _now()
        db.commit()
        return RetryItemResponse(job_id=job.job_id, status="transcribed_pending")
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
