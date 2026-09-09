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
from sqlalchemy import func
from sqlalchemy.orm import Session

import storage
from auth import get_current_user, has_art_track_access
from batch_campaigns import _campaign_or_404, _now, _require_manager, _require_scope
from database import (
    BatchCampaign, BatchCampaignAsset, BatchCampaignItem, DeliveryBatch,
    DeliveryBatchItem, Job, JobOutboxEvent, SessionLocal, get_db, get_deliveries_db,
)
from jobs import create_job


router = APIRouter(prefix="/batch", tags=["art-track-campaigns"])
ART_TRACK_LIMIT = min(int(os.environ.get("BATCH_ART_TRACK_ITEM_LIMIT", "500")), 500)
ALLOWED_AUDIO = {".wav": "audio/wav", ".mp3": "audio/mpeg"}
ALLOWED_COVERS = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
DESTINATIONS = {"argentina": "umg.genly.pro", "chile": "umgchile.genly.pro"}
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
    destination_portal: str | None = Field(default=None, max_length=32)
    idempotency_key: str = Field(..., min_length=16, max_length=160)


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
    cover_rows: dict[tuple[str, str], BatchCampaignAsset] = {}
    for cover in body.covers:
        ext = os.path.splitext(cover.filename)[1].lower()
        if ext not in ALLOWED_COVERS:
            raise HTTPException(status_code=400, detail=f"Unsupported cover format: {cover.filename}")
        digest = cover.sha256.lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise HTTPException(status_code=400, detail=f"Invalid SHA-256 for {cover.filename}.")
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
        job.render_params = {**(campaign.default_render_params or {}), "art_track": True, "batch_art_track": True, "cover_asset_id": cover.id, "render_version": 1}
        job.segments_json = []
        event = create_pipeline_outbox_event(db, job=job, purpose="art_track_batch", mp3_path=None, artist=item.artist or "Unknown", style="oscuro", plan="100", tenant_id=campaign.tenant_id,
            pipeline_kwargs={"art_track": True, "segments_override": [], "input_r2_key": item.upload_key, "bg_r2_key": cover.upload_key,
                             "background_path": None, "song_title": item.title or "", "delivery_profile": "both", "umg_spec": umg_spec,
                             "workload_class": "batch", "label_line": (campaign.default_render_params or {}).get("label_line", "")})
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
    _require_scope(current_user); campaign = _campaign_art_or_409(db, campaign_id, current_user)
    destination = (destination_portal or campaign.destination_portal or "argentina").lower()
    if destination not in DESTINATIONS: raise HTTPException(status_code=400, detail="destination_portal must be argentina or chile")
    rows = db.query(BatchCampaignItem, Job).join(Job, Job.campaign_item_id == BatchCampaignItem.id).filter(BatchCampaignItem.campaign_id == campaign.id).all()
    eligible = []; blocked = []
    for item, job in rows:
        if job.status == "done" and job.approved_at and job.render_params and job.render_params.get("art_track"):
            eligible.append({"item_id": item.id, "job_id": job.job_id, "fingerprint": _fingerprint(job), "title": job.song_title, "artist": job.artist})
        else: blocked.append({"item_id": item.id, "job_id": job.job_id, "reason": "approval_required" if job.status != "done" or not job.approved_at else "not_art_track"})
    return {"destination_portal": destination, "hostname": DESTINATIONS[destination], "eligible": eligible, "blocked": blocked, "eligible_count": len(eligible)}


@router.post("/art-track-campaigns/{campaign_id}/deliveries")
@router.post("/campaigns/{campaign_id}/deliveries")
def create_delivery_batch(campaign_id: str, body: DeliveryCreate, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db), ddb: Session = Depends(get_deliveries_db)):
    _require_scope(current_user); campaign = _campaign_art_or_409(db, campaign_id, current_user); _require_manager(campaign, current_user)
    destination = (body.destination_portal or campaign.destination_portal or "argentina").lower()
    if destination not in DESTINATIONS: raise HTTPException(status_code=400, detail="destination_portal must be argentina or chile")
    existing = db.query(DeliveryBatch).filter(DeliveryBatch.campaign_id == campaign.id, DeliveryBatch.idempotency_key == body.idempotency_key).first()
    if existing:
        return {"operation_id": existing.id, "status": existing.status, "deduplicated": True}
    query = db.query(BatchCampaignItem, Job).join(Job, Job.campaign_item_id == BatchCampaignItem.id).filter(BatchCampaignItem.campaign_id == campaign.id)
    if body.item_ids is not None: query = query.filter(BatchCampaignItem.id.in_(body.item_ids))
    selected = query.with_for_update().all()
    candidates = [(item, job) for item, job in selected if job.status == "done" and job.approved_at and (job.render_params or {}).get("art_track")]
    if not candidates: raise HTTPException(status_code=409, detail={"code": "no_approved_art_tracks"})
    operation = DeliveryBatch(id=str(uuid.uuid4()), campaign_id=campaign.id, tenant_id=campaign.tenant_id, destination_portal=destination, status="queued", idempotency_key=body.idempotency_key, created_by=current_user["id"], total_count=len(candidates), created_at=_now(), updated_at=_now())
    db.add(operation); db.flush()
    for item, job in candidates:
        db.add(DeliveryBatchItem(id=str(uuid.uuid4()), delivery_batch_id=operation.id, campaign_id=campaign.id, job_id=job.job_id, approved_render_fingerprint=_fingerprint(job), status="pending", created_at=_now(), updated_at=_now()))
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
    operation = db.query(DeliveryBatch).filter(
        DeliveryBatch.id == operation_id,
        DeliveryBatch.tenant_id == current_user["tenant_id"],
    ).first()
    if operation is None:
        raise HTTPException(status_code=404, detail="Delivery operation not found.")
    rows = db.query(DeliveryBatchItem).filter(
        DeliveryBatchItem.delivery_batch_id == operation.id,
    ).order_by(DeliveryBatchItem.created_at.asc()).all()
    return {
        "operation_id": operation.id, "status": operation.status,
        "destination_portal": operation.destination_portal,
        "hostname": DESTINATIONS.get(operation.destination_portal),
        "total_count": operation.total_count, "sent_count": operation.sent_count,
        "failed_count": operation.failed_count,
        "items": [{"job_id": row.job_id, "status": row.status,
                    "delivery_id": row.delivery_id, "attempts": row.attempts,
                    "error_code": row.error_code, "receipt": row.receipt} for row in rows],
    }


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


def process_delivery_batch(operation_id: str) -> dict[str, int]:
    """Worker entry point; safe to call repeatedly after a crash."""
    from database import Delivery
    db = SessionLocal(); sent = failed = 0
    try:
        op = db.query(DeliveryBatch).filter(DeliveryBatch.id == operation_id).with_for_update().first()
        if not op: return {"sent": 0, "failed": 0}
        op.status = "sending"; db.commit()
        items = db.query(DeliveryBatchItem).filter(DeliveryBatchItem.delivery_batch_id == op.id, DeliveryBatchItem.status.in_(("pending", "failed"))).with_for_update(skip_locked=True).all()
        from database import DeliveriesSessionLocal, deliveries_added_by
        ddb = DeliveriesSessionLocal()
        try:
            for row in items:
                job = db.query(Job).filter(Job.job_id == row.job_id, Job.tenant_id == op.tenant_id).one_or_none()
                if not job or job.status != "done" or not job.approved_at or _fingerprint(job) != row.approved_render_fingerprint:
                    row.status = "failed"; row.error_code = "stale_approval"; row.error_detail = "Approval or render version changed."; row.attempts = int(row.attempts or 0) + 1; failed += 1; continue
                if storage.is_enabled():
                    missing = [ft for ft in ("video", "short", "thumbnail") if not (job.s3_keys or {}).get(ft) or not storage.object_exists((job.s3_keys or {}).get(ft))]
                    if missing:
                        row.status = "failed"; row.error_code = "deliverables_not_ready"; row.error_detail = ", ".join(missing); row.attempts = int(row.attempts or 0) + 1; failed += 1; continue
                # Never write an AR/CL operation through the legacy
                # single-portal schema. Without the portal_id migration,
                # doing so would make a Chile delivery visible in Argentina
                # (or vice versa). The durable item stays failed and can be
                # retried after the shared Delivery contract is integrated.
                if not hasattr(Delivery, "portal_id"):
                    row.status = "failed"; row.error_code = "portal_contract_unavailable"; row.error_detail = "Delivery.portal_id is required for art-track portal isolation."; row.attempts = int(row.attempts or 0) + 1; failed += 1; continue
                delivery_query = ddb.query(Delivery).filter(Delivery.job_id == job.job_id, Delivery.removed_at.is_(None))
                # Nagoya's portal contract adds portal_id to Delivery. Keep
                # this code compatible with the legacy single-portal schema
                # until that migration is integrated; once present, AR and
                # CL are correctly independent rows.
                if hasattr(Delivery, "portal_id"):
                    delivery_query = delivery_query.filter(Delivery.portal_id == op.destination_portal)
                active = delivery_query.first()
                if active is None:
                    delivery_kwargs = dict(job_id=job.job_id, label="Art Track", file_types=["video", "short", "thumbnail"], artist_snapshot=job.artist, song_title_snapshot=job.song_title or "", tenant_snapshot=job.tenant_id, added_by_user_id=deliveries_added_by(op.created_by), added_at=_now())
                    if hasattr(Delivery, "portal_id"):
                        delivery_kwargs["portal_id"] = op.destination_portal
                    active = Delivery(**delivery_kwargs)
                    ddb.add(active); ddb.flush()
                row.delivery_id = active.id; row.status = "sent"; row.receipt = {"delivery_id": active.id, "portal": op.destination_portal}; row.attempts = int(row.attempts or 0) + 1; sent += 1
            ddb.commit()
        finally: ddb.close()
        op.sent_count = (op.sent_count or 0) + sent; op.failed_count = (op.failed_count or 0) + failed
        remaining = db.query(func.count(DeliveryBatchItem.id)).filter(DeliveryBatchItem.delivery_batch_id == op.id, DeliveryBatchItem.status.in_(("pending", "failed"))).scalar() or 0
        op.status = "completed" if remaining == 0 else "partial"; op.completed_at = _now() if remaining == 0 else None; op.updated_at = _now(); db.commit()
        return {"sent": sent, "failed": failed}
    finally: db.close()
