"""Art-track batch import, association and publication orchestration.

This module deliberately sits beside the legacy lyric campaign routes.  The
existing campaign API remains backwards compatible, while art-track routes
use a separate audio/cover asset table and never enter the transcription
queue.
"""

from __future__ import annotations

import math
import os
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

import logging

import delivery_freshness
import storage
import delivery_freshness
from auth import get_current_user, has_art_track_access
from batch_campaigns import _campaign_or_404, _now, _require_manager, _require_scope
from database import (
    AuditLog, BatchCampaign, BatchCampaignAsset, BatchCampaignItem, DeliveryBatch,
    DeliveryBatchItem, Job, JobOutboxEvent, SessionLocal, get_db, get_deliveries_db,
)
from jobs import create_job
from queue_jobs import enqueue_prores_prewarm


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/batch", tags=["art-track-campaigns"])
ART_TRACK_LIMIT = min(int(os.environ.get("BATCH_ART_TRACK_ITEM_LIMIT", "500")), 500)
ALLOWED_AUDIO = {".wav": "audio/wav", ".mp3": "audio/mpeg"}
ALLOWED_COVERS = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
MAX_COVER_BYTES = int(os.environ.get("BATCH_MAX_COVER_BYTES", str(50 * 1024 * 1024)))
MAX_COVER_PIXELS = int(os.environ.get("BATCH_MAX_COVER_PIXELS", "50000000"))
DESTINATIONS = {"argentina": "umg.genly.pro", "chile": "umgchile.genly.pro"}
DELIVERY_FILE_TYPES = ["umg_master", "video", "umg_short", "short", "thumbnail"]
_SEP_RE = re.compile(r"[^a-z0-9]+")
_CODE_RE = re.compile(r"(?i)(?:^|[^a-z0-9])([a-z]{2,8}[-_ ]?\d{3,})(?:$|[^a-z0-9])")
_COVER_WORDS = {"cover", "front", "album", "artwork", "folder", "caratula", "portada"}
_bearer = HTTPBearer(auto_error=False)


def art_track_feature_enabled() -> bool:
    """Independent kill-switch; unset follows the existing campaign flag."""
    raw = os.environ.get("BATCH_ART_TRACK_ENABLED")
    if raw is None:
        from batch_campaigns import feature_enabled
        return feature_enabled()
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _norm(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode()
    return _SEP_RE.sub(" ", text.casefold()).strip()


def _stem(path: str) -> str:
    return _norm(os.path.splitext(os.path.basename(path))[0])


def _parent(path: str) -> str:
    return _norm(os.path.dirname(path).replace("\\", "/"))


def _code(value: str | None) -> str | None:
    match = _CODE_RE.search(str(value or ""))
    return re.sub(r"[-_ ]", "", match.group(1)).upper() if match else None


class ArtCover(BaseModel):
    filename: str = Field(..., min_length=1, max_length=1000)
    relative_path: str | None = Field(default=None, max_length=1000)
    sha256: str = Field(..., min_length=64, max_length=64)
    size_bytes: int = Field(..., gt=0)
    width: int | None = Field(default=None, ge=1, le=20000)
    height: int | None = Field(default=None, ge=1, le=20000)
    mime_type: str | None = Field(default=None, max_length=120)


class ArtAudio(BaseModel):
    client_id: str | None = Field(default=None, max_length=100)
    filename: str = Field(..., min_length=1, max_length=1000)
    relative_path: str | None = Field(default=None, max_length=1000)
    title: str | None = Field(default=None, max_length=500)
    artist: str | None = Field(default=None, max_length=255)
    technical_code: str | None = Field(default=None, max_length=64)
    album_id: str | None = Field(default=None, max_length=160)
    cover_file: str | None = Field(default=None, max_length=1000)
    size_bytes: int = Field(..., gt=0)
    duration_seconds: float | None = Field(default=None, ge=0)
    sha256: str = Field(..., min_length=64, max_length=64)


class ArtManifest(BaseModel):
    audios: list[ArtAudio] = Field(..., min_length=1, max_length=ART_TRACK_LIMIT)
    covers: list[ArtCover] = Field(default_factory=list, max_length=ART_TRACK_LIMIT)


class AssociationConfirmation(BaseModel):
    item_ids: list[str] | None = Field(default=None, max_length=ART_TRACK_LIMIT)
    confirm_all_matched: bool = False


class AssociationPatch(BaseModel):
    cover_asset_id: str | None = None


class DeliveryCreate(BaseModel):
    item_ids: list[str] | None = Field(default=None, max_length=ART_TRACK_LIMIT)
    job_ids: list[str] | None = Field(default=None, max_length=1000)
    destination_portal: str | None = Field(default=None, max_length=32)
    idempotency_key: str = Field(..., min_length=16, max_length=160)
    # job_id -> client request ids this send was reviewed to close. Nothing is
    # closed unless listed here; the worker re-validates every id.
    resolve_requests: dict[str, list[int]] | None = None
    resolution_note: str | None = Field(default=None, max_length=2000)


def _asset_key(asset: BatchCampaignAsset) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", asset.filename).strip("._") or "asset"
    return f"campaign-assets/{asset.tenant_id}/{asset.campaign_id}/{asset.role}/{asset.sha256}/{safe}"


def _asset(db: Session, campaign: BatchCampaign, asset_id: str, token: str | None) -> BatchCampaignAsset:
    row = db.query(BatchCampaignAsset).filter(
        BatchCampaignAsset.id == asset_id,
        BatchCampaignAsset.campaign_id == campaign.id,
        BatchCampaignAsset.tenant_id == campaign.tenant_id,
    ).with_for_update().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Campaign asset not found.")
    return row


def _match_audio(audio: ArtAudio, covers: list[ArtCover]) -> tuple[ArtCover | None, str, str | None]:
    if not covers:
        return None, "missing", "No covers were included."
    by_path = [c for c in covers if c.relative_path and audio.cover_file and _norm(c.relative_path) == _norm(audio.cover_file)]
    if len(by_path) == 1:
        return by_path[0], "manifest", None
    if len(by_path) > 1:
        return None, "ambiguous", "cover_file names more than one cover"
    wanted_code = _code(audio.technical_code) or _code(audio.filename)
    by_code = [c for c in covers if wanted_code and _code(c.filename) == wanted_code]
    if len(by_code) == 1:
        return by_code[0], "code", None
    if len(by_code) > 1:
        return None, "ambiguous", "More than one cover has the same technical code."
    exact = [c for c in covers if _stem(c.relative_path or c.filename) == _stem(audio.relative_path or audio.filename)]
    if len(exact) == 1:
        return exact[0], "filename", None
    if len(exact) > 1:
        return None, "ambiguous", "More than one cover matches the normalized filename."
    parent = _parent(audio.relative_path or audio.filename)
    shared = [c for c in covers if _parent(c.relative_path or c.filename) == parent and _stem(c.relative_path or c.filename).split()[-1:] and _stem(c.relative_path or c.filename).split()[-1] in _COVER_WORDS]
    if len(shared) == 1:
        return shared[0], "shared_folder", None
    candidates = [c for c in covers if _parent(c.relative_path or c.filename) == parent]
    if len(candidates) > 1:
        return None, "ambiguous", "Multiple covers exist in the audio folder."
    return None, "missing", "No unambiguous cover match."


def _campaign_art_or_409(db: Session, campaign_id: str, user: dict) -> BatchCampaign:
    if not art_track_feature_enabled():
        raise HTTPException(status_code=404, detail="Art-track campaigns are not enabled.")
    if user.get("role") != "uploader" and not has_art_track_access(user):
        raise HTTPException(status_code=403, detail="Art Track is not enabled for this account.")
    campaign = _campaign_or_404(db, campaign_id, user)
    if campaign.kind != "art_track":
        raise HTTPException(status_code=409, detail="This campaign is not an art-track campaign.")
    return campaign


def _campaign_for_delivery(db: Session, campaign_id: str, user: dict) -> BatchCampaign:
    """Return any enabled campaign that can publish approved video jobs.

    Art-track ingestion keeps its own feature and account gate, while normal
    lyric-video campaigns use the existing batch campaign scope. Both share
    the durable delivery operation and portal isolation contract.
    """
    campaign = _campaign_or_404(db, campaign_id, user)
    if campaign.kind == "art_track":
        return _campaign_art_or_409(db, campaign_id, user)
    if campaign.kind != "lyric_video":
        raise HTTPException(status_code=409, detail="This campaign cannot publish videos.")
    return campaign


def _asset_auth(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> dict:
    """Accept either the campaign-only uploader token or normal JWT auth."""
    batch_token = request.headers.get("X-Batch-Upload-Token")
    if batch_token:
        from batch_campaigns import _upload_session_or_401
        session = _upload_session_or_401(db, batch_token)
        return {"tenant_id": session.tenant_id, "campaign_id": session.campaign_id, "id": session.created_by, "role": "uploader"}
    from auth import get_current_user
    return get_current_user(request, credentials, db)


@router.post("/art-track-campaigns/{campaign_id}/manifest")
def register_art_manifest(
    campaign_id: str,
    body: ArtManifest,
    current_user: dict = Depends(_asset_auth),
    db: Session = Depends(get_db),
):
    if not art_track_feature_enabled():
        raise HTTPException(status_code=404, detail="Art-track campaigns are not enabled.")
    if current_user.get("role") != "uploader":
        _require_scope(current_user)
    if current_user.get("role") != "uploader" and not has_art_track_access(current_user):
        raise HTTPException(status_code=403, detail="Art Track is not enabled for this account.")
    campaign = _campaign_art_or_409(db, campaign_id, current_user)
    if current_user.get("role") == "uploader" and current_user.get("campaign_id") != campaign.id:
        raise HTTPException(status_code=404, detail="Campaign not found.")
    _require_manager(campaign, current_user)
    existing = db.query(BatchCampaignItem).filter(BatchCampaignItem.campaign_id == campaign.id).count()
    if existing + len(body.audios) > ART_TRACK_LIMIT:
        raise HTTPException(status_code=413, detail=f"Art-track campaigns accept at most {ART_TRACK_LIMIT} audios.")
    # Surface duplicate technical codes as a deterministic association
    # conflict instead of letting the database uniqueness constraint turn a
    # 500-file manifest into an opaque 500 response. Repeating the exact same
    # audio is still idempotent; reusing a code for different bytes is not.
    incoming_codes: dict[str, str] = {}
    for audio in body.audios:
        code = str(audio.technical_code or "").strip().upper()
        digest = audio.sha256.lower()
        if not code:
            continue
        previous = incoming_codes.get(code)
        if previous is not None and previous != digest:
            raise HTTPException(
                status_code=409,
                detail={"code": "duplicate_technical_code", "technical_code": code},
            )
        incoming_codes[code] = digest
    if incoming_codes:
        existing_codes = {
            row.technical_code: row.sha256
            for row in db.query(BatchCampaignItem).filter(
                BatchCampaignItem.campaign_id == campaign.id,
                BatchCampaignItem.technical_code.in_(incoming_codes),
            ).all()
        }
        for code, digest in incoming_codes.items():
            if code in existing_codes and existing_codes[code] != digest:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "duplicate_technical_code", "technical_code": code},
                )
    cover_rows: dict[tuple[str, str], BatchCampaignAsset] = {}
    for cover in body.covers:
        ext = os.path.splitext(cover.filename)[1].lower()
        if ext not in ALLOWED_COVERS:
            raise HTTPException(status_code=400, detail=f"Unsupported cover format: {cover.filename}")
        digest = cover.sha256.lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise HTTPException(status_code=400, detail=f"Invalid SHA-256 for {cover.filename}.")
        if cover.size_bytes > MAX_COVER_BYTES:
            raise HTTPException(status_code=413, detail=f"Cover is too large: {cover.filename}")
        if cover.width and cover.height and cover.width * cover.height > MAX_COVER_PIXELS:
            raise HTTPException(status_code=413, detail=f"Cover has too many pixels: {cover.filename}")
        key = ("cover", digest)
        row = db.query(BatchCampaignAsset).filter(
            BatchCampaignAsset.campaign_id == campaign.id,
            BatchCampaignAsset.role == "cover",
            BatchCampaignAsset.sha256 == digest,
        ).first()
        if row is None:
            row = BatchCampaignAsset(
                id=str(uuid.uuid4()), campaign_id=campaign.id, tenant_id=campaign.tenant_id,
                role="cover", filename=cover.filename, relative_path=cover.relative_path,
                sha256=digest, size_bytes=cover.size_bytes,
                mime_type=cover.mime_type or ALLOWED_COVERS[ext], width=cover.width, height=cover.height,
                upload_state="registered", created_at=_now(), updated_at=_now(),
            )
            db.add(row); db.flush()
        cover_rows[key] = row
    results = []
    next_ordinal = existing
    for audio in body.audios:
        ext = os.path.splitext(audio.filename)[1].lower()
        if ext not in ALLOWED_AUDIO:
            raise HTTPException(status_code=400, detail=f"Unsupported audio format: {audio.filename}")
        digest = audio.sha256.lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise HTTPException(status_code=400, detail=f"Invalid SHA-256 for {audio.filename}.")
        if audio.size_bytes > int(os.environ.get("BATCH_MAX_AUDIO_BYTES", str(500 * 1024 * 1024))):
            raise HTTPException(status_code=413, detail=f"Audio is too large: {audio.filename}")
        if audio.duration_seconds is not None and (
            audio.duration_seconds <= 0 or audio.duration_seconds > float(
                os.environ.get("BATCH_MAX_AUDIO_DURATION", "3600")
            )
        ):
            raise HTTPException(status_code=422, detail=f"Invalid audio duration: {audio.filename}")
        row = db.query(BatchCampaignItem).filter(
            BatchCampaignItem.campaign_id == campaign.id,
            BatchCampaignItem.sha256 == digest,
        ).first()
        if row is not None:
            results.append({"client_id": audio.client_id, "item_id": row.id, "duplicate": True, "cover_match_state": row.cover_match_state})
            continue
        asset = db.query(BatchCampaignAsset).filter(
            BatchCampaignAsset.campaign_id == campaign.id,
            BatchCampaignAsset.role == "audio", BatchCampaignAsset.sha256 == digest,
        ).first()
        if asset is None:
            asset = BatchCampaignAsset(
                id=str(uuid.uuid4()), campaign_id=campaign.id, tenant_id=campaign.tenant_id,
                role="audio", filename=audio.filename, relative_path=audio.relative_path,
                sha256=digest, size_bytes=audio.size_bytes, mime_type=ALLOWED_AUDIO[ext],
                duration_seconds=audio.duration_seconds, upload_state="registered",
                created_at=_now(), updated_at=_now(),
            )
            db.add(asset); db.flush()
        matched, method, error = _match_audio(audio, body.covers)
        cover_asset = cover_rows.get(("cover", matched.sha256.lower())) if matched else None
        next_ordinal += 1
        metadata_error = None if audio.title and audio.artist else "missing_metadata"
        row = BatchCampaignItem(
            id=str(uuid.uuid4()), campaign_id=campaign.id, tenant_id=campaign.tenant_id,
            ordinal=next_ordinal, filename=audio.filename, title=(audio.title or "").strip() or None,
            artist=(audio.artist or "").strip() or None, technical_code=(audio.technical_code or "").strip().upper() or None,
            size_bytes=audio.size_bytes, duration_seconds=audio.duration_seconds, sha256=digest,
            metadata_error=metadata_error, upload_state="registered", upload_key=asset.upload_key,
            cover_asset_id=cover_asset.id if cover_asset else None, cover_match_state="matched" if cover_asset else method,
            cover_match_method=method, cover_match_error=error, association_confirmed=False,
            created_at=_now(), updated_at=_now(),
        )
        db.add(row)
        results.append({"client_id": audio.client_id, "item_id": row.id, "audio_asset_id": asset.id,
                        "cover_asset_id": cover_asset.id if cover_asset else None,
                        "cover_match_state": row.cover_match_state, "cover_match_method": method,
                        "cover_match_error": error, "duplicate": False})
    campaign.expected_count = max(campaign.expected_count or 0, next_ordinal)
    campaign.updated_at = _now()
    db.commit()
    return {"items": results, "registered_count": next_ordinal, "cover_count": len(cover_rows),
            "matched_count": sum(1 for r in results if r.get("cover_match_state") == "matched"),
            "needs_review_count": sum(1 for r in results if r.get("cover_match_state") in {"ambiguous", "missing"})}


@router.get("/art-track-campaigns/{campaign_id}/assets")
def list_art_assets(campaign_id: str, role: str | None = Query(default=None), current_user: dict = Depends(_asset_auth), db: Session = Depends(get_db)):
    if current_user.get("role") != "uploader": _require_scope(current_user)
    campaign = _campaign_art_or_409(db, campaign_id, current_user)
    if current_user.get("role") == "uploader" and current_user.get("campaign_id") != campaign.id:
        raise HTTPException(status_code=404, detail="Campaign not found.")
    query = db.query(BatchCampaignAsset).filter(BatchCampaignAsset.campaign_id == campaign.id)
    if role:
        if role not in {"audio", "cover"}: raise HTTPException(status_code=400, detail="role must be audio or cover")
        query = query.filter(BatchCampaignAsset.role == role)
    rows = query.order_by(BatchCampaignAsset.created_at.asc()).all()
    return {"items": [{"id": r.id, "role": r.role, "filename": r.filename, "relative_path": r.relative_path,
                        "sha256": r.sha256, "size_bytes": r.size_bytes, "upload_state": r.upload_state,
                        "upload_key": r.upload_key, "width": r.width, "height": r.height} for r in rows]}


@router.post("/art-track-assets/{asset_id}/ticket")
def art_asset_ticket(asset_id: str, current_user: dict = Depends(_asset_auth), db: Session = Depends(get_db)):
    # Art manifests are account-authenticated; the short uploader token is
    # accepted too, but is checked against the same campaign and tenant.
    asset_row = db.query(BatchCampaignAsset).filter(BatchCampaignAsset.id == asset_id).with_for_update().first()
    if asset_row is None: raise HTTPException(status_code=404, detail="Campaign asset not found.")
    campaign = db.query(BatchCampaign).filter(BatchCampaign.id == asset_row.campaign_id).first()
    if campaign is None: raise HTTPException(status_code=404, detail="Campaign not found.")
    if current_user.get("role") == "uploader" and current_user.get("campaign_id") != campaign.id:
        raise HTTPException(status_code=404, detail="Campaign asset not found.")
    if current_user.get("tenant_id") != campaign.tenant_id and current_user.get("role") != "admin":
        raise HTTPException(status_code=404, detail="Campaign asset not found.")
    if asset_row.upload_state == "uploaded": return {"complete": True, "key": asset_row.upload_key}
    ext = os.path.splitext(asset_row.filename)[1].lower()
    content_type = (ALLOWED_AUDIO | ALLOWED_COVERS).get(ext)
    if not content_type: raise HTTPException(status_code=400, detail="Unsupported asset format.")
    key = asset_row.upload_key or _asset_key(asset_row)
    asset_row.upload_key = key; asset_row.upload_state = "uploading"; asset_row.upload_attempts = int(asset_row.upload_attempts or 0) + 1
    multipart = asset_row.size_bytes >= int(os.environ.get("MULTIPART_THRESHOLD_BYTES", str(16 * 1024 * 1024)))
    response: dict[str, Any] = {"complete": False, "key": key, "content_type": content_type, "use_multipart": multipart}
    if multipart:
        part_size = int(os.environ.get("MULTIPART_PART_SIZE_BYTES", str(8 * 1024 * 1024)))
        upload_id = asset_row.multipart_upload_id
        uploaded_parts = None
        if upload_id:
            uploaded_parts = storage.multipart_list_parts(key, upload_id)
            if uploaded_parts is None:
                storage.multipart_abort(key, upload_id)
                upload_id = None
                asset_row.multipart_upload_id = None
        if upload_id is None:
            started = storage.multipart_init_object_key(key, content_type=content_type)
            if not started: raise HTTPException(status_code=503, detail="Could not start multipart upload.")
            upload_id = started["upload_id"]; asset_row.multipart_upload_id = upload_id; uploaded_parts = []
        response.update({"upload_id": upload_id, "part_size": part_size, "uploaded_parts": uploaded_parts or [],
                         "parts": [{"part_number": n, "url": storage.multipart_presign_part(key, upload_id, n, expiry_seconds=3600)}
                                   for n in range(1, math.ceil(asset_row.size_bytes / part_size) + 1)]})
    else:
        signed = storage.presign_put_object_key(key, content_type=content_type, expiry_seconds=900)
        if not signed: raise HTTPException(status_code=503, detail="Could not sign upload URL.")
        response["upload_url"] = signed["url"]
    db.commit(); return response


class AssetComplete(BaseModel):
    parts: list[dict[str, Any]] = Field(default_factory=list, max_length=10000)


@router.post("/art-track-assets/{asset_id}/complete")
def art_asset_complete(asset_id: str, body: AssetComplete, current_user: dict = Depends(_asset_auth), db: Session = Depends(get_db)):
    asset_row = db.query(BatchCampaignAsset).filter(BatchCampaignAsset.id == asset_id).with_for_update().first()
    if asset_row is None: raise HTTPException(status_code=404, detail="Campaign asset not found.")
    campaign = db.query(BatchCampaign).filter(BatchCampaign.id == asset_row.campaign_id).first()
    if campaign is None or (campaign.tenant_id != current_user.get("tenant_id") and current_user.get("role") != "admin"):
        raise HTTPException(status_code=404, detail="Campaign asset not found.")
    if current_user.get("role") == "uploader" and current_user.get("campaign_id") != campaign.id:
        raise HTTPException(status_code=404, detail="Campaign asset not found.")
    if asset_row.upload_state == "uploaded": return {"ok": True, "deduplicated": True}
    if not asset_row.upload_key: raise HTTPException(status_code=409, detail="Upload ticket was not created.")
    if asset_row.multipart_upload_id:
        parts = [{"PartNumber": int(p.get("part_number") or p.get("PartNumber") or 0), "ETag": str(p.get("etag") or p.get("ETag") or "").strip('"')} for p in body.parts]
        if not parts or any(p["PartNumber"] < 1 or not p["ETag"] for p in parts): raise HTTPException(status_code=400, detail="Invalid multipart completion payload.")
        storage.multipart_complete(asset_row.upload_key, asset_row.multipart_upload_id, parts)
    real_size = storage.head_object_size(asset_row.upload_key)
    if real_size is None or int(real_size) != int(asset_row.size_bytes):
        asset_row.upload_state = "error"; asset_row.upload_error = "uploaded_size_mismatch"; db.commit()
        raise HTTPException(status_code=409, detail="Uploaded file size does not match manifest.")
    asset_row.upload_state = "uploaded"; asset_row.multipart_upload_id = None; asset_row.uploaded_at = _now(); asset_row.updated_at = _now()
    if asset_row.role == "audio":
        db.query(BatchCampaignItem).filter(BatchCampaignItem.campaign_id == campaign.id, BatchCampaignItem.sha256 == asset_row.sha256).update({"upload_state": "uploaded", "upload_key": asset_row.upload_key}, synchronize_session=False)
    db.commit(); return {"ok": True, "size_bytes": real_size}


@router.post("/art-track-campaigns/{campaign_id}/associations/confirm")
def confirm_associations(campaign_id: str, body: AssociationConfirmation, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_scope(current_user); campaign = _campaign_art_or_409(db, campaign_id, current_user); _require_manager(campaign, current_user)
    if campaign.status != "active":
        raise HTTPException(status_code=409, detail="Campaign is not active.")
    query = db.query(BatchCampaignItem).filter(BatchCampaignItem.campaign_id == campaign.id)
    if body.item_ids is not None: query = query.filter(BatchCampaignItem.id.in_(body.item_ids))
    rows = query.with_for_update().all()
    if body.confirm_all_matched and body.item_ids is None: rows = [r for r in rows if r.cover_match_state == "matched"]
    blocked = [r.id for r in rows if r.cover_match_state != "matched" or not r.cover_asset_id]
    if blocked: raise HTTPException(status_code=409, detail={"code": "cover_association_unresolved", "item_ids": blocked})
    for row in rows: row.association_confirmed = True; row.updated_at = _now()
    db.commit(); return {"confirmed_count": len(rows), "item_ids": [r.id for r in rows]}


@router.patch("/art-track-campaigns/{campaign_id}/associations/{item_id}")
def patch_association(campaign_id: str, item_id: str, body: AssociationPatch, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_scope(current_user); campaign = _campaign_art_or_409(db, campaign_id, current_user); _require_manager(campaign, current_user)
    if campaign.status != "active":
        raise HTTPException(status_code=409, detail="Campaign is not active.")
    item = db.query(BatchCampaignItem).filter(BatchCampaignItem.id == item_id, BatchCampaignItem.campaign_id == campaign.id).with_for_update().first()
    if item is None:
        raise HTTPException(status_code=404, detail="Campaign item not found.")
    if body.cover_asset_id is None:
        item.cover_asset_id = None
        item.cover_match_state = "missing"
        item.cover_match_method = "manual"
        item.cover_match_error = "Cover removed; choose one before generating."
    else:
        cover = db.query(BatchCampaignAsset).filter(BatchCampaignAsset.id == body.cover_asset_id, BatchCampaignAsset.campaign_id == campaign.id, BatchCampaignAsset.role == "cover").first()
        if cover is None:
            raise HTTPException(status_code=404, detail="Cover asset not found.")
        item.cover_asset_id = cover.id
        item.cover_match_state = "matched"
        item.cover_match_method = "manual"
        item.cover_match_error = None
        item.association_confirmed = False
    item.updated_at = _now()
    db.commit()
    return {"item_id": item.id, "cover_asset_id": item.cover_asset_id, "cover_match_state": item.cover_match_state, "association_confirmed": item.association_confirmed}


@router.post("/art-track-campaigns/{campaign_id}/start-rendering")
def start_art_rendering(campaign_id: str, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_scope(current_user)
    if not art_track_feature_enabled(): raise HTTPException(status_code=404, detail="Art-track campaigns are not enabled.")
    if not has_art_track_access(current_user): raise HTTPException(status_code=403, detail="Art Track is not enabled for this account.")
    campaign = _campaign_art_or_409(db, campaign_id, current_user); _require_manager(campaign, current_user)
    if campaign.status != "active": raise HTTPException(status_code=409, detail="Campaign is not active.")
    art_track_preset = (campaign.default_render_params or {}).get("art_track_preset", "waveform")
    if art_track_preset not in ("waveform", "colombia_static"):
        raise HTTPException(status_code=422, detail="Unknown Art Track visual preset.")
    rows = db.query(BatchCampaignItem).filter(BatchCampaignItem.campaign_id == campaign.id).order_by(BatchCampaignItem.ordinal).with_for_update().all()
    cover_ids = {r.cover_asset_id for r in rows if r.cover_asset_id}
    covers = {
        cover.id: cover for cover in db.query(BatchCampaignAsset).filter(
            BatchCampaignAsset.id.in_(cover_ids or {"_none_"}),
            BatchCampaignAsset.role == "cover",
        ).all()
    }
    eligible = [
        r for r in rows
        if r.association_confirmed and r.cover_asset_id and r.upload_state == "uploaded"
        and r.metadata_error not in {"invalid_size", "invalid_duration"}
        and covers.get(r.cover_asset_id) is not None
        and covers[r.cover_asset_id].upload_state == "uploaded"
    ]
    blocked = [r.id for r in rows if r not in eligible]
    if not eligible: raise HTTPException(status_code=409, detail={"code": "no_art_tracks_ready", "blocked_item_ids": blocked})
    from transactional_outbox import create_pipeline_outbox_event
    # Render UMG-compatible intermediates once; the existing publisher can
    # later materialize ProRes without re-rendering the art track.
    umg_spec = {"frame_size": "HD", "fps": 24.0, "prores_profile": 3}
    created = []; events = []
    for item in eligible:
        if db.query(Job).filter(Job.campaign_item_id == item.id).first(): continue
        cover = db.query(BatchCampaignAsset).filter(BatchCampaignAsset.id == item.cover_asset_id).one()
        job_id = create_job(db, artist=item.artist or "Unknown", song_title=item.title or "", style="oscuro", filename=item.filename,
                            user_id=campaign.created_by, tenant_id=campaign.tenant_id, delivery_profile="both", umg_spec=umg_spec,
                            initial_status="transcribed_pending", input_r2_key=item.upload_key, workload_class="batch",
                            campaign_id=campaign.id, campaign_item_id=item.id, commit=False)
        job = db.query(Job).filter(Job.job_id == job_id).one()
        job.render_params = {**(campaign.default_render_params or {}), "art_track": True, "art_track_preset": art_track_preset, "batch_art_track": True, "cover_asset_id": cover.id, "render_version": 1}
        job.segments_json = []
        event = create_pipeline_outbox_event(db, job=job, purpose="art_track_batch", mp3_path=None, artist=item.artist or "Unknown", style="oscuro", plan="100", tenant_id=campaign.tenant_id,
            pipeline_kwargs={"art_track": True, "segments_override": [], "input_r2_key": item.upload_key, "bg_r2_key": cover.upload_key,
                             "background_path": None, "song_title": item.title or "", "delivery_profile": "both", "umg_spec": umg_spec,
                             "label_line": (campaign.default_render_params or {}).get("label_line", ""),
                             "art_track_preset": art_track_preset})
        job.status = "queued"; job.current_step = "queued"; job.progress = 0
        created.append(job_id); events.append(event.id)
    db.commit()
    event_ids = reconcile_art_track_campaign(db, campaign)
    db.commit()
    from transactional_outbox import dispatch_outbox_event
    dispatched = sum(1 for event_id in event_ids if dispatch_outbox_event(event_id).get("status") == "dispatched")
    return {"campaign_id": campaign.id, "created_count": len(created), "job_ids": created, "dispatched_count": dispatched, "blocked_item_ids": blocked}


def reconcile_art_track_campaign(db: Session, campaign: BatchCampaign) -> list[str]:
    """Select the next bounded set of art-track outbox events.

    The manifest can contain 500 approved associations, but only the shared
    batch render window is dispatched at once. Events not selected remain
    pending and are picked up by the normal campaign reconciler after each
    render completes, so two campaigns cannot bypass the global buffer.
    """
    if campaign.kind != "art_track" or campaign.status != "active":
        return []
    active = db.query(func.count(Job.id)).filter(
        Job.tenant_id == campaign.tenant_id, Job.workload_class == "batch",
        Job.status.in_(("queued", "processing", "rendering", "editing", "background_generating")),
    ).scalar() or 0
    review = db.query(func.count(Job.id)).filter(
        Job.tenant_id == campaign.tenant_id, Job.workload_class == "batch",
        Job.status == "pending_review",
    ).scalar() or 0
    room = min(max(0, int(os.environ.get("BATCH_RENDER_WINDOW", "10")) - active),
               max(0, int(os.environ.get("BATCH_FINAL_REVIEW_LIMIT", "50")) - review - active))
    if room <= 0:
        return []
    rows = db.query(JobOutboxEvent).join(Job, Job.job_id == JobOutboxEvent.job_id).filter(
        Job.campaign_id == campaign.id, Job.workload_class == "batch", Job.status == "queued",
        JobOutboxEvent.event_type == "pipeline.enqueue", JobOutboxEvent.status == "pending",
    ).order_by(Job.created_at.asc()).with_for_update(skip_locked=True).limit(room).all()
    return [row.id for row in rows]


def _fingerprint(job: Job) -> str:
    import hashlib, json
    payload = {"job_id": job.job_id, "audio": job.input_audio_sha256 or job.input_r2_key, "cover": (job.render_params or {}).get("cover_asset_id"), "render": job.render_params or {}, "s3": job.s3_keys or {}}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


@router.post("/art-track-campaigns/{campaign_id}/delivery-preview")
@router.post("/campaigns/{campaign_id}/deliveries/preview")
def delivery_preview(campaign_id: str, destination_portal: str | None = Query(default=None), current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_scope(current_user); campaign = _campaign_for_delivery(db, campaign_id, current_user)
    destination = (destination_portal or campaign.destination_portal or "argentina").lower()
    if destination not in DESTINATIONS: raise HTTPException(status_code=400, detail="destination_portal must be argentina or chile")
    if campaign.kind == "art_track":
        rows = db.query(BatchCampaignItem, Job).join(Job, Job.campaign_item_id == BatchCampaignItem.id).filter(BatchCampaignItem.campaign_id == campaign.id).all()
    else:
        rows = [(None, job) for job in db.query(Job).filter(
            Job.campaign_id == campaign.id, Job.tenant_id == campaign.tenant_id,
        ).order_by(Job.created_at.asc()).all()]
    eligible = []; blocked = []
    for item, job in rows:
        approved = job.status == "done" and job.approved_at and bool(job.video_url)
        if campaign.kind == "art_track":
            approved = approved and bool(job.render_params and job.render_params.get("art_track"))
        if approved:
            eligible.append({"item_id": item.id if item else None, "job_id": job.job_id, "fingerprint": _fingerprint(job), "title": job.song_title, "artist": job.artist})
        else:
            reason = "approval_required" if job.status != "done" or not job.approved_at else "deliverables_not_ready"
            if campaign.kind == "art_track" and job.status == "done" and job.approved_at and not (job.render_params or {}).get("art_track"):
                reason = "not_art_track"
            blocked.append({"item_id": item.id if item else None, "job_id": job.job_id, "reason": reason})
    return {"destination_portal": destination, "hostname": DESTINATIONS[destination], "eligible": eligible, "blocked": blocked, "eligible_count": len(eligible)}


def _validated_close_intents(db, ddb, campaign, candidates, destination, body) -> dict[str, dict]:
    """Which client requests each selected song may close when it is published.

    Rejects the whole send (nothing is created) if any id is not closable by
    THIS cut in THIS portal, so the operator's ticks never silently turn into
    something else. The worker checks again before resolving.
    """
    if not body.resolve_requests or not any(body.resolve_requests.values()):
        return {}
    if len(body.resolve_requests) > 200 or sum(len(ids) for ids in body.resolve_requests.values()) > 500:
        raise HTTPException(status_code=422, detail={"code": "too_many_change_requests"})
    from campaign_change_requests import actions_enabled, closable_on_publish
    if campaign.kind == "art_track" or not actions_enabled():
        raise HTTPException(status_code=409, detail={"code": "feature_disabled"})
    from database import DeliveryChangeRequest, EditorDocument
    from delivery_replacement import target
    by_job = {job.job_id: job for _, job in candidates}
    note = (body.resolution_note or "").strip() or None
    intents: dict[str, dict] = {}
    for job_id, raw_ids in body.resolve_requests.items():
        ids = sorted(set(raw_ids))
        if not ids:
            continue
        job = by_job.get(job_id)
        if job is None:
            raise HTTPException(status_code=409, detail={"code": "change_request_job_not_in_send", "job_id": job_id})
        active, _duplicate = target(db, ddb, job, destination)
        if active is None:
            raise HTTPException(status_code=409, detail={"code": "change_request_not_closable", "job_id": job_id, "reason": "no_delivery_in_portal"})
        document = db.query(EditorDocument).filter(EditorDocument.job_id == job.job_id).first()
        found = {row.id: row for row in ddb.query(DeliveryChangeRequest).filter(
            DeliveryChangeRequest.id.in_(ids), DeliveryChangeRequest.delivery_id == active.id).all()}
        for request_id in ids:
            row = found.get(request_id)
            ok, reason = closable_on_publish(row, job, active, document) if row is not None else (False, "not_found")
            if not ok:
                raise HTTPException(status_code=409, detail={"code": "change_request_not_closable", "job_id": job_id, "request_id": request_id, "reason": reason})
        intents[job_id] = {"request_ids": ids, "segments_revision": job.segments_revision, "note": note}
    return intents


@router.post("/art-track-campaigns/{campaign_id}/deliveries")
@router.post("/campaigns/{campaign_id}/deliveries")
def create_delivery_batch(campaign_id: str, body: DeliveryCreate, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db), ddb: Session = Depends(get_deliveries_db)):
    _require_scope(current_user); campaign = _campaign_for_delivery(db, campaign_id, current_user); _require_manager(campaign, current_user)
    if body.item_ids is not None and body.job_ids is not None:
        raise HTTPException(status_code=400, detail="Elegí item_ids o job_ids, no ambos.")
    destination = (body.destination_portal or campaign.destination_portal or "argentina").lower()
    if destination not in DESTINATIONS: raise HTTPException(status_code=400, detail="destination_portal must be argentina or chile")
    # The UI locks the portal of a campaign that has one; the API must too, or a
    # stale tab / script can publish a Chile campaign into the Argentina portal.
    locked_portal = (campaign.destination_portal or "").lower()
    if locked_portal in DESTINATIONS and body.destination_portal and body.destination_portal.lower() != locked_portal:
        raise HTTPException(status_code=409, detail={"code": "portal_locked", "portal": locked_portal})
    existing = db.query(DeliveryBatch).filter(DeliveryBatch.campaign_id == campaign.id, DeliveryBatch.idempotency_key == body.idempotency_key).first()
    if existing:
        return {"operation_id": existing.id, "status": existing.status, "deduplicated": True}
    if campaign.kind == "art_track":
        query = db.query(BatchCampaignItem, Job).join(Job, Job.campaign_item_id == BatchCampaignItem.id).filter(BatchCampaignItem.campaign_id == campaign.id)
        if body.item_ids is not None: query = query.filter(BatchCampaignItem.id.in_(body.item_ids))
        selected = query.with_for_update().all()
    else:
        query = db.query(Job).filter(Job.campaign_id == campaign.id, Job.tenant_id == campaign.tenant_id)
        if body.job_ids is not None: query = query.filter(Job.job_id.in_(body.job_ids))
        selected = [(None, job) for job in query.with_for_update().all()]
    candidates = []
    for item, job in selected:
        approved = job.status == "done" and job.approved_at and bool(job.video_url)
        if campaign.kind == "art_track":
            approved = approved and bool((job.render_params or {}).get("art_track"))
        if approved:
            candidates.append((item, job))
    if not candidates:
        code = "no_approved_art_tracks" if campaign.kind == "art_track" else "no_approved_videos"
        raise HTTPException(status_code=409, detail={"code": code})
    intents = _validated_close_intents(db, ddb, campaign, candidates, destination, body)
    operation = DeliveryBatch(id=str(uuid.uuid4()), campaign_id=campaign.id, tenant_id=campaign.tenant_id, destination_portal=destination, status="queued", idempotency_key=body.idempotency_key, created_by=current_user["id"], total_count=len(candidates), created_at=_now(), updated_at=_now())
    db.add(operation); db.flush()
    for item, job in candidates:
        db.add(DeliveryBatchItem(id=str(uuid.uuid4()), delivery_batch_id=operation.id, campaign_id=campaign.id, job_id=job.job_id, approved_render_fingerprint=_fingerprint(job), status="pending", change_request_intent=intents.get(job.job_id), created_at=_now(), updated_at=_now()))
    db.commit()
    # Publication itself is deliberately a durable operation. A later worker
    # can call process_delivery_batch; the request never fires 500 network
    # calls and closing the browser cannot cancel the snapshot.
    scheduled = enqueue_delivery_batch(operation.id)
    return JSONResponse(status_code=202, content={"operation_id": operation.id, "status": operation.status, "destination_portal": destination, "hostname": DESTINATIONS[destination], "total_count": operation.total_count, "scheduled": scheduled})


@router.get("/art-track-delivery-operations/{operation_id}")
@router.get("/delivery-operations/{operation_id}")
def get_delivery_batch(operation_id: str, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_scope(current_user)
    query = db.query(DeliveryBatch).filter(DeliveryBatch.id == operation_id)
    if current_user.get("role") != "admin":
        query = query.filter(DeliveryBatch.tenant_id == current_user["tenant_id"])
    operation = query.first()
    if operation is None:
        raise HTTPException(status_code=404, detail="Delivery operation not found.")
    # Use the same campaign authorization as creation: a platform admin may
    # publish for another tenant and must be able to read that operation.
    campaign = _campaign_for_delivery(db, operation.campaign_id, current_user)
    if campaign.tenant_id != operation.tenant_id:
        raise HTTPException(status_code=404, detail="Delivery operation not found.")
    rows = db.query(DeliveryBatchItem).filter(
        DeliveryBatchItem.delivery_batch_id == operation.id,
    ).order_by(DeliveryBatchItem.created_at.asc()).all()
    # Old workers counted before flushing item transitions (autoflush=False),
    # leaving a one-item successful operation permanently "partial".
    sent_count = sum(row.status == 'sent' for row in rows)
    failed_count = sum(row.status == 'failed' for row in rows)
    status = 'completed' if rows and sent_count == len(rows) else operation.status
    return {
        "operation_id": operation.id, "status": status,
        "destination_portal": operation.destination_portal,
        "hostname": DESTINATIONS.get(operation.destination_portal),
        "total_count": operation.total_count, "sent_count": sent_count,
        "failed_count": failed_count,
        "items": [{"job_id": row.job_id, "status": row.status,
                    "delivery_id": row.delivery_id, "attempts": row.attempts,
                    "error_code": row.error_code, "error_detail": row.error_detail,
                    "retryable": row.status == "failed" and row.error_code not in NON_RETRYABLE_ITEM_ERRORS,
                    "receipt": row.receipt} for row in rows],
        "stalled": _operation_stalled(operation, rows),
    }


# A retry cannot fix these: they need a person (pick the delivery to correct,
# integrate the portal contract) or a new approval, not another attempt.
NON_RETRYABLE_ITEM_ERRORS = {"ambiguous_replacement", "portal_contract_unavailable", "stale_approval"}
STALL_AFTER_SECONDS = 600


def _operation_stalled(operation: DeliveryBatch, rows) -> bool:
    """Queued/sending with unfinished items and no progress for a while.

    Redis down at enqueue time, or a worker killed mid-operation, leaves the
    operation in flight forever; the UI uses this to offer a real retry.
    """
    if operation.status not in ("queued", "sending"):
        return False
    if not any(row.status in ("pending", "failed") for row in rows):
        return False
    updated = operation.updated_at
    if updated is None:
        return False
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return (_now() - updated).total_seconds() > STALL_AFTER_SECONDS


def _delivery_batch_rq_state(operation_id: str) -> str | None:
    """RQ status of the operation's job, None when it does not exist / no Redis."""
    try:
        from queue_jobs import _init_redis
        from rq.job import Job as RqJob
        from rq.exceptions import NoSuchJobError
        redis, _, _ = _init_redis()
        if redis is None:
            return "unknown"
        try:
            return RqJob.fetch(f"delivery-batch:{operation_id}", connection=redis).get_status(refresh=True)
        except NoSuchJobError:
            return None
        except Exception:
            # Redis unreachable is NOT "no job": never enqueue on a guess.
            return "unknown"
    except Exception:
        return "unknown"


def requeue_delivery_batch(operation_id: str) -> str:
    """Re-schedule an operation. Returns queued | already_running | unavailable.

    process_delivery_batch is idempotent (sent items are skipped), so the only
    hazard is running two workers on the same operation: never enqueue while an
    RQ job for it is still queued or running.
    """
    state = _delivery_batch_rq_state(operation_id)
    if state in ("queued", "started", "scheduled", "deferred"):
        return "already_running"
    if state == "unknown":
        return "unavailable"
    try:
        from queue_jobs import _init_redis
        from rq.job import Job as RqJob
        redis, _, _ = _init_redis()
        if redis is not None:
            try:
                RqJob.fetch(f"delivery-batch:{operation_id}", connection=redis).delete()
            except Exception:
                pass
    except Exception:
        pass
    return "queued" if enqueue_delivery_batch(operation_id) else "unavailable"


def reconcile_stalled_delivery_batches(db: Session, limit: int = 20) -> int:
    """Re-schedule operations left queued/sending by a lost enqueue or a dead worker.

    Called from the campaign reconciler. Safe to run every tick: an operation is
    only touched when it has unfinished items, has not moved for
    STALL_AFTER_SECONDS and has no live RQ job.
    """
    from datetime import timedelta
    cutoff = _now() - timedelta(seconds=STALL_AFTER_SECONDS)
    candidates = db.query(DeliveryBatch).filter(
        DeliveryBatch.status.in_(("queued", "sending")), DeliveryBatch.updated_at < cutoff,
    ).order_by(DeliveryBatch.updated_at.asc()).limit(limit).with_for_update(skip_locked=True).all()
    requeued = 0
    for operation in candidates:
        unfinished = db.query(func.count(DeliveryBatchItem.id)).filter(
            DeliveryBatchItem.delivery_batch_id == operation.id,
            DeliveryBatchItem.status.in_(("pending", "failed")),
        ).scalar() or 0
        if not unfinished:
            continue
        outcome = requeue_delivery_batch(operation.id)
        if outcome == "queued":
            operation.status = "queued"; operation.updated_at = _now()
            db.add(AuditLog(user_id=None, action="delivery.batch_requeued", detail={"delivery_batch_id": operation.id, "campaign_id": operation.campaign_id, "source": "reconciler", "unfinished_items": int(unfinished)}))
            db.commit(); requeued += 1
        else:
            # Still running, or Redis unreachable: look again later, and do not let
            # operations that cannot be re-queued starve the ones behind them.
            operation.updated_at = _now(); db.commit()
    return requeued


@router.post("/art-track-delivery-operations/{operation_id}/retry")
@router.post("/delivery-operations/{operation_id}/retry")
def retry_delivery_batch(operation_id: str, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """Retry the failed or never-processed items of an operation."""
    _require_scope(current_user)
    query = db.query(DeliveryBatch).filter(DeliveryBatch.id == operation_id)
    if current_user.get("role") != "admin":
        query = query.filter(DeliveryBatch.tenant_id == current_user["tenant_id"])
    operation = query.with_for_update().first()
    if operation is None:
        raise HTTPException(status_code=404, detail="Delivery operation not found.")
    campaign = _campaign_for_delivery(db, operation.campaign_id, current_user)
    if campaign.tenant_id != operation.tenant_id:
        raise HTTPException(status_code=404, detail="Delivery operation not found.")
    _require_manager(campaign, current_user)
    retryable = db.query(func.count(DeliveryBatchItem.id)).filter(
        DeliveryBatchItem.delivery_batch_id == operation.id,
        or_(DeliveryBatchItem.status == "pending",
            and_(DeliveryBatchItem.status == "failed",
                 or_(DeliveryBatchItem.error_code.is_(None), ~DeliveryBatchItem.error_code.in_(tuple(NON_RETRYABLE_ITEM_ERRORS))))),
    ).scalar() or 0
    if not retryable:
        raise HTTPException(status_code=409, detail={"code": "nothing_to_retry"})
    if operation.status == "sending" and (_now() - (operation.updated_at if operation.updated_at.tzinfo else operation.updated_at.replace(tzinfo=timezone.utc))).total_seconds() <= STALL_AFTER_SECONDS:
        raise HTTPException(status_code=409, detail={"code": "operation_in_progress"})
    outcome = requeue_delivery_batch(operation.id)
    if outcome == "queued":
        operation.status = "queued"; operation.updated_at = _now(); db.commit()
    db.add(AuditLog(user_id=current_user["id"], action="delivery.batch_retry", detail={"delivery_batch_id": operation.id, "campaign_id": campaign.id, "outcome": outcome, "retryable_items": int(retryable)}))
    db.commit()
    return JSONResponse(status_code=202, content={"operation_id": operation.id, "status": operation.status, "scheduled": outcome == "queued", "outcome": outcome})


def enqueue_delivery_batch(operation_id: str) -> bool:
    """Schedule durable publication without making the browser the worker."""
    try:
        from queue_jobs import _init_redis, rq_payload_metadata
        from rq import Queue
        redis, _, _ = _init_redis()
        if redis is None:
            return False
        Queue("campaign_control", connection=redis).enqueue(
            process_delivery_batch, operation_id, job_id=f"delivery-batch:{operation_id}",
            job_timeout=3600, result_ttl=3600, failure_ttl=86400,
            meta=rq_payload_metadata("campaign_control"),
        )
        return True
    except Exception:
        return False


def _evaluate_close_intent(db, ddb, row, job, active, changed) -> tuple[list, list[dict]]:
    """Requests the operator ticked that this publication may really close.

    Returns (rows to resolve, skipped with reason). Skipping is never an error:
    the song still publishes, the request just stays open and the receipt says
    why.
    """
    intent = row.change_request_intent or {}
    ids = intent.get("request_ids") or []
    if not ids:
        return [], []
    if active is None or not changed:
        return [], [{"id": rid, "reason": "content_unchanged" if active is not None else "no_delivery"} for rid in ids]
    if intent.get("segments_revision") != job.segments_revision:
        return [], [{"id": rid, "reason": "cut_changed_after_review"} for rid in ids]
    from campaign_change_requests import closable_on_publish
    from database import DeliveryChangeRequest, EditorDocument
    document = db.query(EditorDocument).filter(EditorDocument.job_id == job.job_id).first()
    resolvable, skipped = [], []
    for rid in ids:
        request = ddb.query(DeliveryChangeRequest).filter(
            DeliveryChangeRequest.id == rid, DeliveryChangeRequest.delivery_id == active.id,
        ).populate_existing().with_for_update().first()
        ok, reason = closable_on_publish(request, job, active, document) if request is not None else (False, "not_found")
        if ok:
            resolvable.append(request)
        else:
            skipped.append({"id": rid, "reason": reason})
    return resolvable, skipped


def process_delivery_batch(operation_id: str) -> dict[str, int]:
    """Worker entry point; safe to call repeatedly after a crash."""
    from database import Delivery
    db = SessionLocal(); sent = failed = 0; current_row_id = None
    try:
        op = db.query(DeliveryBatch).filter(DeliveryBatch.id == operation_id).with_for_update().first()
        if not op: return {"sent": 0, "failed": 0}
        campaign = db.query(BatchCampaign).filter(BatchCampaign.id == op.campaign_id).first()
        delivery_label = "Art Track" if campaign and campaign.kind == "art_track" else "Campaña"
        op.status = "sending"; db.commit()
        items = db.query(DeliveryBatchItem).filter(DeliveryBatchItem.delivery_batch_id == op.id, DeliveryBatchItem.status.in_(("pending", "failed"))).order_by(DeliveryBatchItem.created_at.asc(), DeliveryBatchItem.id.asc()).with_for_update(skip_locked=True).all()
        from database import DeliveriesSessionLocal, deliveries_added_by
        ddb = DeliveriesSessionLocal()
        try:
            for item in items:
                # Persist what the previous song left pending (a failure mark set
                # on a `continue` path) before this song can roll anything back.
                db.commit(); ddb.commit()
                row = item
                current_row_id = item.id
                try:
                    current_row_id = row.id
                    job = db.query(Job).filter(Job.job_id == row.job_id, Job.tenant_id == op.tenant_id).with_for_update().one_or_none()
                    if not job or job.status != "done" or not job.approved_at or _fingerprint(job) != row.approved_render_fingerprint:
                        row.status = "failed"; row.error_code = "stale_approval"; row.error_detail = "Approval or render version changed."; row.attempts = int(row.attempts or 0) + 1; failed += 1; continue
                    if storage.is_enabled():
                        # The MP4/short/thumbnail are produced by the render.
                        missing = [ft for ft in ("video", "short", "thumbnail") if not (job.s3_keys or {}).get(ft) or not storage.object_exists((job.s3_keys or {}).get(ft))]
                        if missing:
                            row.status = "failed"; row.error_code = "deliverables_not_ready"; row.error_detail = ", ".join(missing); row.attempts = int(row.attempts or 0) + 1; failed += 1; continue
                    # ProRes NO se materializa solo. Esto publicaba los dos .mov en
                    # `file_types` sin verificarlos, apoyado en que el portal los
                    # transcodifica al primer download — y no lo hace: el portal
                    # firma la key determinística de R2 y nunca pasa por
                    # `ensure_prores_exists` (documentado desde el incidente
                    # 2026-08-03). Resultado medido el 2026-09-15 en el portal de
                    # Chile: 28 de 34 entregas activas ofrecían un "ProRes Master
                    # (broadcast)" que no existe en R2 y que nada iba a crear.
                    #
                    # Un job sin `umg_spec` no puede producirlos (no hay frame
                    # size, fps ni perfil), así que se publica como entrega
                    # PARCIAL —igual que un job sin short vertical— en vez de
                    # prometer un archivo inexistente. Uno con spec sí puede: se
                    # encola el prewarm y se publica; el archivo aparece cuando el
                    # transcode termina.
                    delivery_file_types = list(DELIVERY_FILE_TYPES)
                    prores_absent = [
                        ft for ft in ("umg_master", "umg_short")
                        if not storage.is_enabled()
                        or not (job.s3_keys or {}).get(ft)
                        or not storage.object_exists((job.s3_keys or {}).get(ft))
                    ]
                    if prores_absent and not job.umg_spec:
                        delivery_file_types = [ft for ft in delivery_file_types if ft not in prores_absent]
                        logger.warning(
                            "[DELIVERY] job=%s se publica SIN %s: no tiene umg_spec, "
                            "nada los puede generar", job.job_id, sorted(prores_absent),
                        )
                    elif prores_absent:
                        # SIN force: la ruta masiva puede publicar hasta 500
                        # canciones de una, y `force=True` saltea a propósito el
                        # tope de profundidad de cola. 1000 transcodes de varios
                        # GB encolados de un saque se ponen delante de TODOS los
                        # renders de cliente que vengan después, en la misma cola
                        # `enterprise`. Acá no hay nadie esperando el archivo: si
                        # la cola está llena, que la auditoría diaria lo reporte y
                        # se pida bajo demanda desde el portal, que sí es un click
                        # humano y sí justifica saltear el tope.
                        for ft in prores_absent:
                            try:
                                enqueue_prores_prewarm(job.job_id, ft)
                            except Exception as exc:
                                logger.warning(
                                    "[DELIVERY] no se pudo encolar %s de job=%s: %s",
                                    ft, job.job_id, exc,
                                )
                        row.status = 'failed'; row.error_code = 'deliverables_not_ready'
                        row.error_detail = 'Esperando el archivo profesional. Reintentá cuando termine.'
                        row.attempts = int(row.attempts or 0) + 1; failed += 1
                        continue
                    # Never write an AR/CL operation through the legacy
                    # single-portal schema. Without the portal_id migration,
                    # doing so would make a Chile delivery visible in Argentina
                    # (or vice versa). The durable item stays failed and can be
                    # retried after the shared Delivery contract is integrated.
                    if not hasattr(Delivery, "portal_id"):
                        row.status = "failed"; row.error_code = "portal_contract_unavailable"; row.error_detail = "Delivery.portal_id is required for art-track portal isolation."; row.attempts = int(row.attempts or 0) + 1; failed += 1; continue
                    from delivery_replacement import target, identity, archive_duplicate
                    try:
                        active, duplicate = target(db, ddb, job, op.destination_portal)
                    except HTTPException as exc:
                        # Ambiguous same-song replacement: one song must not abort
                        # the whole operation. It needs a human decision, not a retry.
                        row.status = "failed"; row.error_code = "ambiguous_replacement"
                        row.error_detail = str(exc.detail)[:500]
                        row.attempts = int(row.attempts or 0) + 1; failed += 1
                        db.commit()
                        continue
                    replaced_job_id = active.job_id if active and active.job_id != job.job_id else None
                    previous_publication = {'job_id': active.job_id, 'revision': active.published_revision,
                                            'file_keys': active.published_file_keys} if replaced_job_id else None
                    changed = bool(replaced_job_id) or (delivery_freshness.needs_publish(job, active) if active else False)
                    from delivery_snapshots import copy_snapshot, latest_pointer_enabled
                    pinned = active.published_file_keys if active and not changed and active.file_types == delivery_file_types else None
                    try:
                        if not pinned:
                            expected = (identity(active), identity(duplicate))
                            expected_job = (_fingerprint(job), job.segments_revision, job.approved_at)
                            tenant, jid, item_id, portal = job.tenant_id, job.job_id, row.id, op.destination_portal
                            # Persist prior item results before releasing both
                            # transactions. Multi-GB copies exceed DB idle limits.
                            ddb.commit()
                            db.commit()
                            pinned = None if latest_pointer_enabled() else copy_snapshot(tenant, jid, delivery_file_types)
                            row = db.query(DeliveryBatchItem).filter_by(id=item_id).populate_existing().with_for_update().one()
                            if row.status == 'sent':
                                continue
                            job = db.query(Job).filter_by(job_id=jid).populate_existing().with_for_update().one()
                            active, duplicate = target(db, ddb, job, portal)
                            for delivery in sorted([d for d in (active, duplicate) if d is not None], key=lambda d: d.id):
                                ddb.refresh(delivery, with_for_update=True)
                            if (job.status != 'done' or expected_job != (_fingerprint(job), job.segments_revision, job.approved_at)
                                    or expected != (identity(active), identity(duplicate))):
                                row.status = 'failed'; row.error_code = 'stale_approval'
                                row.error_detail = 'El corte o la publicación cambiaron durante el envío. Revisá y reintentá.'
                                row.attempts = int(row.attempts or 0) + 1; failed += 1
                                continue
                    except Exception:
                        row.status = 'failed'; row.error_code = 'deliverables_not_ready'
                        row.error_detail = 'No se pudo preparar la publicación; el portal no se modificó.'
                        row.attempts = int(row.attempts or 0) + 1; failed += 1
                        continue
                    is_new_delivery = active is None
                    # Decide BEFORE touching the delivery: once its fingerprint is
                    # updated the corrected cut no longer reads as "needs publish".
                    resolvable, skipped_requests = _evaluate_close_intent(db, ddb, row, job, active, changed)
                    if active is None:
                        # Las columnas de frescura se escriben también acá. Sin
                        # esto, TODA fila publicada por campaña nacía sin
                        # fingerprint, así que la detección de deriva quedaba
                        # muerta justo en las filas que lista la campaña — y la
                        # primera corrección de cada una pasaba en silencio.
                        delivery_kwargs = dict(job_id=job.job_id, label=delivery_label, file_types=delivery_file_types, artist_snapshot=job.artist, song_title_snapshot=job.song_title or "", tenant_snapshot=job.tenant_id, added_by_user_id=deliveries_added_by(op.created_by), added_at=_now(), frame_size_snapshot=(job.umg_spec or {}).get("frame_size"), published_render_fingerprint=delivery_freshness.render_fingerprint(job), content_updated_at=_now())
                        if hasattr(Delivery, "portal_id"):
                            delivery_kwargs["portal_id"] = op.destination_portal
                        active = Delivery(**delivery_kwargs)
                        ddb.add(active); ddb.flush()
                    else:
                        active.job_id = job.job_id
                        active.label = delivery_label
                        active.file_types = delivery_file_types
                        # Re-publicar por campaña: mismo criterio que el alta y que
                        # el endpoint individual. Y limpiar la ventana de "en
                        # vuelo": si no, una fila marcada al pedir el re-render se
                        # queda diciéndole "actualizando" al cliente para siempre.
                        # OJO con el nombre: `_fingerprint` ya es una función de
                        # este módulo (la del snapshot de aprobación) y una local
                        # con ese nombre la sombrea en TODO el scope, rompiendo su
                        # uso de más arriba con UnboundLocalError.
                        _render_fp = delivery_freshness.render_fingerprint(job)
                        active.published_render_fingerprint = _render_fp
                        active.stale_since = None
                        active.stale_reason = None
                        active.artist_snapshot = job.artist
                        active.song_title_snapshot = job.song_title or ""
                        active.tenant_snapshot = job.tenant_id
                        active.frame_size_snapshot = (job.umg_spec or {}).get("frame_size")
                        active.added_by_user_id = deliveries_added_by(op.created_by)
                        active.added_at = _now()
                    row.delivery_id = active.id; row.status = "sent"; row.receipt = {"delivery_id": active.id, "portal": op.destination_portal, "replaced_job_id": replaced_job_id, "previous_publication": previous_publication, **({"change_requests": {"resolved": [r.id for r in resolvable], "skipped": skipped_requests}} if row.change_request_intent else {})}; row.attempts = int(row.attempts or 0) + 1; sent += 1
                    archive_duplicate(duplicate, _now())
                    active.published_file_keys = pinned
                    active.published_render_fingerprint = delivery_freshness.render_fingerprint(job)
                    active.stale_since = None; active.stale_reason = None
                    if changed:
                        active.published_revision = (active.published_revision or 1) + 1
                        active.approved_at = None; active.approved_by_label = None
                    if changed or active.content_updated_at is None:
                        active.content_updated_at = _now()
                    # A campaign send attests the selected cut, not every client
                    # request predating it. Only the requests the operator ticked in
                    # the send (and that still pass the same rule the worker just
                    # re-checked) are closed; everything else stays open.
                    _now_closed = _now()
                    for closed in resolvable:
                        closed.resolved_at = _now_closed; closed.updated_at = _now_closed
                        closed.resolved_by_user_id = deliveries_added_by(op.created_by)
                        closed.resolved_by_revision = active.published_revision
                        closed.resolution_source = "publication"
                        closed.resolution_note = (row.change_request_intent or {}).get("note") or f"Resuelto al publicar la versión {active.published_revision}."
                    row.error_code = None; row.error_detail = None
                    db.add(AuditLog(user_id=op.created_by, action="delivery.create" if is_new_delivery else "delivery.update", detail={
                        "job_id": job.job_id, "label": delivery_label, "portal_id": op.destination_portal,
                        "artist": job.artist, "song": job.song_title, "revision": active.published_revision,
                        "content_changed": changed, "source": "campaign_bulk", "delivery_batch_id": op.id,
                        "replaced_job_id": replaced_job_id, "previous_publication": previous_publication,
                        "resolved_change_requests": [r.id for r in resolvable],
                    }))
                    ddb.commit()
                    db.commit()
                except Exception:
                    # One song must never stop the ones after it.
                    logger.exception("[DELIVERY] batch %s: song item %s failed", operation_id, current_row_id)
                    db.rollback(); ddb.rollback()
                    stuck = db.query(DeliveryBatchItem).filter_by(id=current_row_id).populate_existing().first()
                    if stuck is not None and stuck.status != "sent":
                        stuck.status = "failed"; stuck.error_code = "unexpected_error"
                        stuck.error_detail = "Error inesperado al publicar; reintentá."
                        stuck.attempts = int(stuck.attempts or 0) + 1; failed += 1
                        db.commit()
            ddb.commit()
        except Exception:
            # One unexpected error (DB blip, storage hiccup) must not leave the
            # whole operation "sending" forever with the browser polling it.
            logger.exception("[DELIVERY] batch %s aborted outside a song (last item %s)", operation_id, current_row_id)
            db.rollback(); ddb.rollback()
        finally: ddb.close()
        db.flush()
        op.sent_count = db.query(func.count(DeliveryBatchItem.id)).filter(DeliveryBatchItem.delivery_batch_id == op.id, DeliveryBatchItem.status == 'sent').scalar() or 0
        op.failed_count = db.query(func.count(DeliveryBatchItem.id)).filter(DeliveryBatchItem.delivery_batch_id == op.id, DeliveryBatchItem.status == 'failed').scalar() or 0
        remaining = db.query(func.count(DeliveryBatchItem.id)).filter(DeliveryBatchItem.delivery_batch_id == op.id, DeliveryBatchItem.status.in_(("pending", "failed"))).scalar() or 0
        op.status = "completed" if remaining == 0 else "partial"; op.completed_at = _now() if remaining == 0 else None; op.updated_at = _now(); db.commit()
        return {"sent": sent, "failed": failed}
    finally: db.close()
