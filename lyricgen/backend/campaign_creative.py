"""Campaign creative assignments, immutable receipts and scoped video history.

No providers or queues are called here. Existing JSON columns hold the current
configuration; append-only AuditLog rows hold previews, changes and deliveries.
The campaign row serializes writers and the preview fingerprint detects editor
or job changes before applying a bulk operation.
"""
from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import math
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, File, Form, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy.orm import Session

from auth import get_current_user, has_scenes_access
from database import AuditLog, BackgroundAsset, BatchCampaign, BatchCampaignItem, EditorDocument, Job, get_db
from batch_campaigns import _campaign_or_404, _require_manager, _require_scope, _aware
from campaign_models import veo_models

router = APIRouter(prefix="/batch/campaigns", tags=["campaign-creative"])
KEY = "creative_plan"
ASSIGNMENT = "creative_assignment"
CATALOG = json.loads(Path(__file__).with_name("campaign_creative_catalog.json").read_text())


def field(label, group, kind="text", **kw):
    return {"label": label, "group": group, "kind": kind, **kw}


FIELDS = {
    "font": field("Tipografía", "Letra", "select", options=[""] + CATALOG["fonts"]),
    "font_scale": field("Tamaño de letra", "Letra", "number", min=.6, max=1.5, step=.05),
    "text_case": field("Mayúsculas", "Letra", "select", options=["upper", "lower", "title", "sentence", "original"]),
    "lyric_color": field("Color de letra", "Letra", "color"),
    "lyric_sung_color": field("Color cantado", "Letra", "color"),
    "text_contrast": field("Contraste", "Letra", "select", options=["subtle", "medium", "strong"]),
    "lyrics_animation": field("Animación de letra", "Letra", "select", options=["none", "karaoke", "word_reveal", "pop", "glow"]),
    "line_transition": field("Transición entre líneas", "Letra", "select", options=["none", "slide_up", "slide_side", "wipe", "dissolve_blur"]),
    "background_id": field("Fondo de biblioteca o propio", "Fondo", "asset"),
    "background_mode": field("Uso del fondo", "Fondo", "select", options=["as_is", "variation"]),
    "scene_source": field("Inspiración del fondo", "Fondo", "select", options=["lyrics", "auto", "prompt_literal", "prompt_improved"]),
    "background_hint": field("Prompt", "Fondo", "textarea", max_length=4000),
    "genre": field("Género", "Fondo", max_length=64),
    "concept": field("Concepto", "Fondo", "select", options=[""] + CATALOG["concepts"]),
    "style": field("Paleta", "Fondo", "select", options=["auto", "oscuro", "neon", "minimal", "calido", "custom"]),
    "custom_colors": field("Colores de paleta", "Fondo", max_length=200),
    "movement_style": field("Movimiento", "Movimiento y efectos", "select", options=[""] + CATALOG["movements"]),
    "effect": field("Efecto", "Movimiento y efectos", "select", options=[""] + CATALOG["effects"]),
    "animate_image": field("Animar imagen con IA", "Movimiento y efectos", "boolean"),
    "enable_scenes": field("Varias escenas", "Movimiento y efectos", "boolean"),
    "title_template": field("Disposición de portada", "Portada", "select", options=["auto", "centered", "lower_third", "badge"]),
    "title_size": field("Tamaño de portada", "Portada", "number", min=.5, max=2, step=.05),
    "title_artist_font": field("Tipografía del artista", "Portada", "select", options=[""] + CATALOG["fonts"]),
    "title_song_font": field("Tipografía del título", "Portada", "select", options=[""] + CATALOG["fonts"]),
    "title_song_break": field("Salto de línea del título", "Portada", "textarea", max_length=200),
    "frame_format": field("Formato visual", "Salida", "select", options=["full", "cine"]),
    "delivery_profile": field("Entrega", "Salida", "select", options=["youtube", "umg", "both"]),
    "umg_frame_size": field("Resolución UMG", "Salida", "select", options=["HD", "UHD-4K", "DCI-2K", "DCI-4K"]),
    "umg_fps": field("FPS UMG", "Salida", "select", options=["23.976", "24", "25", "29.97", "30", "50", "59.94", "60"]),
    "umg_prores_profile": field("Perfil ProRes", "Salida", "select", options=["3", "4", "5"]),
}
RENDER_KEYS = (set(FIELDS) - {"scene_source"}) | {"match_lyrics", "bg_verbatim"}
DEFAULTS = {"font": "", "font_scale": 1., "text_case": "upper", "text_contrast": "medium",
            "lyric_color": "#FFFFFF", "lyric_sung_color": "#FFFFFF", "lyrics_animation": "none",
            "line_transition": "none", "background_id": None, "background_mode": "as_is",
            "match_lyrics": True, "bg_verbatim": False, "background_hint": "", "genre": "", "concept": "",
            "movement_style": "", "effect": "", "style": "auto", "custom_colors": "",
            "animate_image": False, "enable_scenes": False, "title_template": "auto", "title_size": 1.,
            "title_artist_font": "", "title_song_font": "", "title_song_break": "", "frame_format": "full",
            "delivery_profile": "youtube", "umg_frame_size": "HD", "umg_fps": "25", "umg_prores_profile": "3"}


def effective_settings(campaign, overrides):
    values = {k: v for k, v in {**DEFAULTS, **(campaign.default_render_params or {}), **(overrides or {})}.items() if k in RENDER_KEYS}
    if values.get("background_mode") not in {"as_is", "variation"}:
        values["background_mode"] = "as_is"
    return values


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def fail(message, code=422):
    raise HTTPException(code, detail=message)


def normalize_settings(settings):
    out = {}
    for key, value in settings.items():
        spec = FIELDS.get(key)
        if not spec:
            fail(f"Ajuste no permitido: {key}")
        kind = spec["kind"]
        if kind == "boolean":
            if type(value) is not bool:
                fail(f"{spec['label']}: se requiere sí/no")
        elif kind == "number":
            if type(value) not in (int, float) or not math.isfinite(value) or not spec["min"] <= value <= spec["max"]:
                fail(f"{spec['label']}: fuera de rango")
        elif kind == "asset":
            if value is not None and (type(value) is not int or value <= 0):
                fail("Fondo no válido")
        else:
            if not isinstance(value, str) or len(value) > spec.get("max_length", 200):
                fail(f"{spec['label']}: valor no válido")
            if kind == "select" and value not in spec["options"]:
                fail(f"{spec['label']}: opción no disponible")
            if kind == "color":
                import re
                if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
                    fail("Color no válido")
        out[key] = value
    source = out.pop("scene_source", None)
    if source:
        out.update(match_lyrics=source == "lyrics", bg_verbatim=source == "prompt_literal")
        if source in {"auto", "lyrics"}:
            out["background_hint"] = ""
        elif not out.get("background_hint", "").strip():
            fail("Ingresá el prompt para este modo de fondo")
    return out


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Group(Strict):
    id: str = Field(min_length=1, max_length=60, pattern=r"^[a-zA-Z0-9_-]+$")
    name: str = Field(min_length=1, max_length=100)
    weight: float = Field(gt=0, le=1000, allow_inf_nan=False)
    requirement: Literal["creative", "photo_effect", "veo"] = "creative"
    model: str = Field(default="", max_length=100)
    settings: dict[str, Any] = Field(default_factory=dict, max_length=40)


class PlanRequest(Strict):
    revision: int = Field(ge=0)
    item_ids: list[str] = Field(min_length=1, max_length=1000)
    mode: Literal["percent", "count"] = "percent"
    groups: list[Group] = Field(min_length=1, max_length=30)
    seed: str = Field(default="campaign", min_length=1, max_length=100)
    replace_exceptions: bool = False
    pin: bool = False
    contract: bool = False
    reason: str = Field(min_length=3, max_length=1000)
    agreement: str = Field(default="", max_length=4000)
    rounding_note: str = Field(default="", max_length=500)


class CommitRequest(Strict):
    preview_id: str


class UndoRequest(Strict):
    revision: int
    operation_id: str
    reason: str = Field(min_length=3, max_length=1000)


def apportion(groups, count, mode):
    weights = [g.weight for g in groups]
    if mode == "count":
        if any(not w.is_integer() for w in weights) or sum(weights) != count:
            fail("Las cantidades deben sumar las canciones disponibles")
        return [int(w) for w in weights], False
    if abs(sum(weights) - 100) > .000001:
        fail("Los porcentajes deben sumar 100")
    raw = [count * w / 100 for w in weights]
    counts = [math.floor(v) for v in raw]
    order = sorted(range(len(raw)), key=lambda i: (-(raw[i] - counts[i]), groups[i].id))
    for i in order[:count - sum(counts)]:
        counts[i] += 1
    return counts, any(abs(v - round(v)) > .000001 for v in raw)


def _campaign(db, campaign_id, user, write=False):
    _require_scope(user)
    campaign = _campaign_or_404(db, campaign_id, user)
    if write:
        _require_manager(campaign, user)
        campaign = db.query(BatchCampaign).filter_by(id=campaign_id).populate_existing().with_for_update().one()
    if campaign.kind != "lyric_video":
        fail("Esta configuración corresponde a campañas de lyrics")
    return campaign


def _plan(campaign):
    return copy.deepcopy((campaign.default_render_params or {}).get(KEY) or {"revision": 0})


def _items(db, campaign):
    return db.query(BatchCampaignItem).filter_by(campaign_id=campaign.id).order_by(BatchCampaignItem.ordinal).all()


def _snapshot(db, campaign):
    jobs = db.query(Job).filter(Job.campaign_id == campaign.id).all()
    docs = db.query(EditorDocument).filter(EditorDocument.job_id.in_([j.job_id for j in jobs])).all()
    return digest({"defaults": campaign.default_render_params,
                   "items": [(i.id, i.render_overrides, i.discard_record) for i in _items(db, campaign)],
                   "jobs": sorted((j.job_id, j.status, j.render_params) for j in jobs),
                   "docs": sorted((d.job_id, d.revision, str(d.lock_expires_at)) for d in docs)})


def _log(db, campaign, action, user_id, detail):
    row = AuditLog(user_id=user_id, action="campaign.creative." + action,
                   detail={"campaign_id": campaign.id, "tenant_id": campaign.tenant_id, **detail})
    db.add(row)
    return row


def _logs(db, campaign, action):
    return db.query(AuditLog).filter(AuditLog.action == "campaign.creative." + action,
        AuditLog.detail["campaign_id"].as_string() == campaign.id).order_by(AuditLog.id.desc())


def validate_combination(db, campaign, settings, group, user):
    asset = None
    if settings.get("background_id"):
        asset = db.query(BackgroundAsset).filter_by(id=settings["background_id"], is_active=True).first()
        if not asset or asset.owner_tenant_id not in (None, campaign.tenant_id):
            fail("Fondo no disponible para esta campaña", 404)
    if settings.get("enable_scenes") and (asset or not has_scenes_access(user)):
        fail("Escenas requiere acceso habilitado y fondo IA")
    if group.requirement == "photo_effect":
        if settings.get("effect") in (None, "", "none", "foto_viva"):
            fail("Foto fija requiere un efecto de superposición; Foto viva no es foto fija")
        if settings.get("animate_image") or settings.get("enable_scenes") or settings.get("background_mode") == "variation":
            fail("Foto fija no admite animación IA, escenas ni variación")
        if asset and asset.file_type not in {"jpg", "jpeg", "png"}:
            fail("El recurso seleccionado no es una foto")
        if settings.get("movement_style") != "foto-parallax":
            fail("Elegí Foto fija en movimiento para este grupo")
    if group.requirement == "veo":
        if group.model not in {m["id"] for m in veo_models()}:
            fail("Elegí uno de los modelos Veo disponibles para el contrato")
        if asset or settings.get("movement_style") in {"", "foto-parallax", None} or settings.get("effect") == "foto_viva":
            fail("Veo requiere fondo IA y movimiento de video explícito")


@router.get("/{campaign_id}/creative")
def get_creative(campaign_id: str, current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    campaign = _campaign(db, campaign_id, current_user)
    jobs = {j.campaign_item_id: j for j in db.query(Job).filter_by(campaign_id=campaign.id).all() if j.campaign_item_id}
    return {"campaign_id": campaign.id, "plan": _plan(campaign), "fields": FIELDS,
            "veo_model": os.environ.get("VEO_MODEL", "veo-3.1-fast-generate-001").strip(),
            "veo_models": veo_models(),
            "can_manage": current_user.get("role") == "admin" or campaign.created_by == current_user.get("id"),
            "items": [{"id": i.id, "ordinal": i.ordinal, "artist": i.artist, "title": i.title or i.filename,
                       "discarded": bool(i.discard_record and not i.discard_record.get("restored_at")),
                       "job_id": jobs[i.id].job_id if i.id in jobs else None,
                       "status": jobs[i.id].status if i.id in jobs else "waiting",
                       "settings": effective_settings(campaign, i.render_overrides),
                       "assignment": (i.render_overrides or {}).get(ASSIGNMENT)} for i in _items(db, campaign)],
            "operations": [{"id": r.detail.get("operation_id"), "at": str(r.created_at), "actor": r.user_id,
                            "reason": r.detail.get("reason"), "revision": r.detail.get("revision")}
                           for r in _logs(db, campaign, "applied").limit(100).all()]}


@router.post("/{campaign_id}/creative/preview")
def preview(campaign_id: str, body: PlanRequest, current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    campaign = _campaign(db, campaign_id, current_user, True)
    plan = _plan(campaign)
    if plan["revision"] != body.revision:
        fail("La configuración cambió. Recargá antes de continuar.", 409)
    if len(set(body.item_ids)) != len(body.item_ids) or len({g.id for g in body.groups}) != len(body.groups):
        fail("La selección o los grupos contienen duplicados")
    selected = [i for i in _items(db, campaign) if i.id in set(body.item_ids)]
    if len(selected) != len(body.item_ids):
        fail("Selección ajena a la campaña", 404)
    if any(i.discard_record and not i.discard_record.get("restored_at") for i in selected):
        fail("La selección incluye canciones descartadas", 409)
    skipped = [i.id for i in selected if (i.render_overrides or {}).get(ASSIGNMENT, {}).get("pinned") and not body.replace_exceptions]
    selected = [i for i in selected if i.id not in skipped]
    if not selected:
        fail("No quedan canciones disponibles; revisá las excepciones fijadas")
    selected_ids = {i.id for i in selected}
    jobs = db.query(Job).filter(Job.campaign_item_id.in_(selected_ids)).all()
    if any(j.status not in {"transcribed_pending", "transcribed", "lyrics_approved", "transcribing", "transcribing_queued", "separation_ready", "separating", "separation_queued"} for j in jobs):
        fail("La selección incluye canciones descartadas, generadas o en generación", 409)
    docs = db.query(EditorDocument).filter(EditorDocument.job_id.in_([j.job_id for j in jobs])).all()
    if any(d.lock_expires_at and _aware(d.lock_expires_at) > datetime.now(timezone.utc) for d in docs):
        fail("Hay canciones abiertas en revisión. Cerrá esas sesiones antes de asignar.", 409)
    counts, rounded = apportion(body.groups, len(selected), body.mode)
    if body.contract and (not body.agreement.strip() or (rounded and not body.rounding_note.strip())):
        fail("Registrá el acuerdo y la aceptación del redondeo cuando corresponda")
    order = sorted(selected, key=lambda i: digest([body.seed, i.id]))
    changes = []
    offset = 0
    for group, count in zip(body.groups, counts):
        patch = normalize_settings(group.settings)
        for item in order[offset:offset + count]:
            before = copy.deepcopy(item.render_overrides or {})
            effective = effective_settings(campaign, {**before, **patch})
            validate_combination(db, campaign, effective, group, current_user)
            assignment = {"group_id": group.id, "group_name": group.name, "requirement": group.requirement,
                          "model": group.model, "pinned": body.pin, "revision": body.revision + 1}
            if group.requirement == "creative" and not body.contract and before.get(ASSIGNMENT):
                assignment = {**before[ASSIGNMENT], "revision": body.revision + 1,
                              "pinned": body.pin or before[ASSIGNMENT].get("pinned", False), "preset_name": group.name}
                retained = Group(id=assignment["group_id"], name=assignment["group_name"], weight=100,
                                 requirement=assignment["requirement"], model=assignment.get("model", ""))
                validate_combination(db, campaign, effective, retained, current_user)
            after = {**before, **patch, ASSIGNMENT: assignment}
            changes.append({"item_id": item.id, "artist": item.artist, "title": item.title or item.filename,
                            "before": before, "after": after, "group": group.name})
        offset += count
    preview_id = str(uuid.uuid4())
    detail = {"preview_id": preview_id, "fingerprint": _snapshot(db, campaign), "request": body.model_dump(),
              "changes": changes, "skipped": skipped, "counts": counts, "rounded": rounded, "created_at": now()}
    _log(db, campaign, "preview", current_user["id"], detail)
    db.commit()
    # Source lyrics stay server-side; the UI only needs the creative diff.
    return {**{k: detail[k] for k in ("preview_id", "counts", "rounded", "skipped")},
            "changes": [{**{k: c[k] for k in ("item_id", "artist", "title", "group")},
                         "before": effective_settings(campaign, c["before"]),
                         "after": effective_settings(campaign, c["after"])} for c in changes]}


@router.post("/{campaign_id}/creative/apply")
def apply(campaign_id: str, body: CommitRequest, current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    campaign = _campaign(db, campaign_id, current_user, True)
    previous = _logs(db, campaign, "applied").filter(AuditLog.detail["operation_id"].as_string() == body.preview_id).first()
    if previous:
        return {"revision": previous.detail["revision"], "operation_id": body.preview_id, "deduplicated": True}
    record = _logs(db, campaign, "preview").filter(AuditLog.detail["preview_id"].as_string() == body.preview_id).first()
    if not record or record.user_id != current_user["id"]:
        fail("Vista previa no encontrada", 404)
    detail = record.detail
    db.query(Job).filter(Job.campaign_id == campaign.id).order_by(Job.job_id).with_for_update().all()
    db.query(EditorDocument).filter(EditorDocument.job_id.in_(
        db.query(Job.job_id).filter(Job.campaign_id == campaign.id))).order_by(EditorDocument.job_id).populate_existing().with_for_update().all()
    if (datetime.now(timezone.utc) - _aware(record.created_at)).total_seconds() > 1800:
        fail("La vista previa venció; volvé a calcularla", 409)
    if _snapshot(db, campaign) != detail["fingerprint"]:
        fail("Cambió una canción o la campaña; revisá una nueva vista previa", 409)
    plan = _plan(campaign)
    old_plan = copy.deepcopy(plan)
    request = detail["request"]
    plan.update(revision=plan["revision"] + 1, groups=request["groups"], mode=request["mode"], updated_at=now())
    if request["contract"]:
        plan["contract"] = {"revision": plan["revision"], "agreement": request["agreement"],
            "rounding_note": request["rounding_note"], "counts": detail["counts"],
            "groups": request["groups"], "mode": request["mode"], "actor": current_user["id"], "at": now(),
            "item_ids": [c["item_id"] for c in detail["changes"]]}
    items = {i.id: i for i in _items(db, campaign)}
    for change in detail["changes"]:
        items[change["item_id"]].render_overrides = change["after"]
    campaign.default_render_params = {**(campaign.default_render_params or {}), KEY: plan}
    _log(db, campaign, "applied", current_user["id"], {"operation_id": body.preview_id, "revision": plan["revision"],
         "before_plan": old_plan, "after_plan": plan, "changes": detail["changes"], "reason": request["reason"]})
    db.commit()
    return {"revision": plan["revision"], "operation_id": body.preview_id}


@router.post("/{campaign_id}/creative/undo")
def undo(campaign_id: str, body: UndoRequest, current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    campaign = _campaign(db, campaign_id, current_user, True)
    plan = _plan(campaign)
    record = _logs(db, campaign, "applied").filter(AuditLog.detail["operation_id"].as_string() == body.operation_id).first()
    if not record or plan["revision"] != body.revision or record.detail["revision"] != body.revision:
        fail("Sólo se puede deshacer la última asignación sin cambios posteriores", 409)
    items = {i.id: i for i in _items(db, campaign)}
    changed = {c["item_id"] for c in record.detail["changes"]}
    jobs = db.query(Job).filter(Job.campaign_item_id.in_(changed)).order_by(Job.job_id).populate_existing().with_for_update().all()
    if any(j.status not in {"transcribed_pending", "transcribed", "lyrics_approved"} for j in jobs):
        fail("Hay trabajos que ya avanzaron; no se puede deshacer", 409)
    docs = db.query(EditorDocument).filter(EditorDocument.job_id.in_([j.job_id for j in jobs])).order_by(EditorDocument.job_id).populate_existing().with_for_update().all()
    if any(d.lock_expires_at and _aware(d.lock_expires_at) > datetime.now(timezone.utc) for d in docs):
        fail("Hay una revisión abierta", 409)
    for c in record.detail["changes"]:
        if items[c["item_id"]].render_overrides != c["after"]:
            fail("Una canción tiene cambios posteriores", 409)
    for c in record.detail["changes"]:
        items[c["item_id"]].render_overrides = c["before"]
    restored = {**record.detail["before_plan"], "revision": plan["revision"] + 1}
    campaign.default_render_params = {**campaign.default_render_params, KEY: restored}
    _log(db, campaign, "undone", current_user["id"], {"operation_id": body.operation_id, "reason": body.reason,
                                                    "before": plan, "after": restored})
    db.commit()
    return {"revision": restored["revision"]}


@router.post("/{campaign_id}/creative/assets")
async def upload_asset(campaign_id: str, file: UploadFile = File(...), name: str = Form(..., max_length=255),
                       current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    campaign = _campaign(db, campaign_id, current_user, True)
    tenant = campaign.tenant_id
    db.rollback()  # Do not hold a campaign lock while validating/uploading media.
    from admin import upload_background
    # The manager is authorized for this campaign only. The destination is
    # server-derived; the shared validator never receives an arbitrary tenant.
    return await upload_background(file=file, name=name, tags="campaign", owner_tenant_id=tenant,
                                   admin=current_user, db=db)


class DeliveryRequest(Strict):
    job_id: str
    video_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    destination: str = Field(min_length=3, max_length=500)
    note: str = Field(default="", max_length=1000)


@router.post("/{campaign_id}/creative/deliveries")
def record_delivery(campaign_id: str, body: DeliveryRequest, current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    campaign = _campaign(db, campaign_id, current_user, True)
    job = next((j for j in campaign_jobs(db, campaign) if j.job_id == body.job_id), None)
    if not job:
        fail("Video no encontrado en esta campaña", 404)
    job = db.query(Job).filter_by(job_id=job.job_id).populate_existing().with_for_update().one()
    evidence = (job.render_params or {}).get("campaign_render_evidence") or {}
    if job.status != "done" or not job.approved_at or not job.video_url or evidence.get("video_sha256") != body.video_sha256:
        fail("La entrega requiere el archivo vigente y aprobado, con huella verificada", 409)
    previous = _logs(db, campaign, "delivered").filter(AuditLog.detail["job_id"].as_string() == job.job_id,
                 AuditLog.detail["video_sha256"].as_string() == body.video_sha256).first()
    if previous:
        return {"ok": True, "deduplicated": True}
    _log(db, campaign, "delivered", current_user["id"], {**body.model_dump(), "at": now(),
         "receipt": (job.render_params or {}).get("campaign_creative_receipt"), "evidence": evidence})
    db.commit()
    return {"ok": True}


def generation_receipt(db, job, submitted_revision, settings, actor):
    """Called in the locked native publication transaction, before outbox."""
    if not job.campaign_id:
        return
    campaign = db.query(BatchCampaign).filter_by(id=job.campaign_id).one()
    item = db.query(BatchCampaignItem).filter_by(id=job.campaign_item_id).first()
    assignment = (item.render_overrides or {}).get(ASSIGNMENT) if item else None
    if not assignment:
        return
    if str(assignment["revision"]) != str(submitted_revision):
        fail("Cambió el estilo asignado a esta canción. Reabrí el editor.", 409)
    group = Group(id=assignment["group_id"], name=assignment["group_name"], weight=100,
                  requirement=assignment["requirement"], model=assignment.get("model", ""))
    validate_combination(db, campaign, settings, group, actor)
    receipt = {"assignment": assignment, "contract": _plan(campaign).get("contract"),
               "settings": settings, "actor": actor["id"], "at": now(), "job_id": job.job_id}
    job.render_params = {**(job.render_params or {}), "campaign_creative_receipt": receipt}
    _log(db, campaign, "generation", actor["id"], receipt)


class IndividualRequest(Strict):
    revision: int = Field(ge=0)
    settings: dict[str, Any]


@router.post("/{campaign_id}/creative/items/{item_id}")
def save_individual(campaign_id: str, item_id: str, body: IndividualRequest,
                    current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    campaign = _campaign(db, campaign_id, current_user)
    campaign = db.query(BatchCampaign).filter_by(id=campaign.id).populate_existing().with_for_update().one()
    item = db.query(BatchCampaignItem).filter_by(id=item_id, campaign_id=campaign.id).first()
    job = db.query(Job).filter_by(campaign_item_id=item_id, campaign_id=campaign.id).with_for_update().first()
    if not item or not job:
        fail("Canción no encontrada", 404)
    if job.status not in {"transcribed_pending", "transcribed", "lyrics_approved"}:
        fail("La canción ya avanzó a generación", 409)
    doc = db.query(EditorDocument).filter_by(job_id=job.job_id).first()
    if doc and doc.lock_expires_at and _aware(doc.lock_expires_at) > datetime.now(timezone.utc) and doc.lock_user_id != current_user["id"]:
        fail("Otra persona está revisando esta canción", 409)
    before = copy.deepcopy(item.render_overrides or {})
    assignment = before.get(ASSIGNMENT) or {}
    if assignment.get("revision", 0) != body.revision:
        fail("La configuración individual cambió; reabrí el editor", 409)
    patch = normalize_settings(body.settings)
    effective = effective_settings(campaign, {**before, **patch})
    group = Group(id=assignment.get("group_id", "individual"), name=assignment.get("group_name", "Individual"),
                  weight=100, requirement=assignment.get("requirement", "creative"), model=assignment.get("model", ""))
    validate_combination(db, campaign, effective, group, current_user)
    # Idempotent autosave/approval retry; an unchanged choice isn't an exception.
    if all(effective.get(k) == effective_settings(campaign, before).get(k) for k in patch):
        return {"revision": body.revision, "unchanged": True}
    plan = _plan(campaign)
    plan["revision"] += 1
    assignment = {**assignment, "group_id": group.id, "group_name": group.name,
                  "requirement": group.requirement, "model": group.model, "revision": plan["revision"], "pinned": True}
    item.render_overrides = {**before, **patch, ASSIGNMENT: assignment}
    campaign.default_render_params = {**(campaign.default_render_params or {}), KEY: plan}
    _log(db, campaign, "individual", current_user["id"], {"item_id": item.id, "before": before,
          "after": item.render_overrides, "revision": plan["revision"]})
    db.commit()
    return {"revision": plan["revision"]}


def campaign_jobs(db, campaign):
    # Include pre-feature variants by ancestry, always intersecting tenant.
    from sqlalchemy import select, or_
    lineage = select(Job.job_id).where(Job.campaign_id == campaign.id, Job.tenant_id == campaign.tenant_id).cte(recursive=True)
    lineage = lineage.union(select(Job.job_id).join(lineage, Job.parent_job_id == lineage.c.job_id).where(
        Job.tenant_id == campaign.tenant_id, or_(Job.campaign_id.is_(None), Job.campaign_id == campaign.id)))
    return db.query(Job).filter(Job.job_id.in_(select(lineage.c.job_id)), Job.tenant_id == campaign.tenant_id).all()


def history_rows(db, campaign):
    rows = []
    for j in sorted(campaign_jobs(db, campaign), key=lambda j: (str(j.created_at), j.job_id), reverse=True):
        rp = j.render_params or {}
        if not j.video_url and not rp.get("campaign_creative_receipt") and j.status not in {"queued", "processing", "rendering", "editing", "pending_review", "done"}:
            continue
        receipt = rp.get("campaign_creative_receipt") or {}
        evidence = rp.get("campaign_render_evidence") or {}
        assignment = receipt.get("assignment") or {}
        outcome = "pending"
        if evidence.get("video_sha256") and j.video_url:
            requirement = assignment.get("requirement")
            if requirement == "photo_effect":
                outcome = "verified" if evidence.get("background_kind") == "image" and evidence.get("effect_applied") not in (None, "", "none", "foto_viva") and not evidence.get("degraded") else "deviation"
            elif requirement == "veo":
                outcome = "verified" if assignment.get("model") in evidence.get("models", []) and evidence.get("background_kind") == "video" and not evidence.get("degraded") else "unverified"
            else:
                outcome = "unverified"
        rows.append({"job_id": j.job_id, "parent_job_id": j.parent_job_id, "item_id": j.campaign_item_id,
                     "artist": j.artist, "title": j.song_title or j.filename, "status": j.status,
                     "created_at": str(j.created_at), "approved_at": str(j.approved_at) if j.approved_at else None,
                     "video_url": f"/download/{j.job_id}/video" if j.video_url else None,
                     "open_path": f"/videos/{j.job_id}", "assignment": assignment,
                     "settings": receipt.get("settings", {}), "evidence": evidence, "compliance": outcome})
    return rows


@router.get("/{campaign_id}/videos")
def video_history(campaign_id: str, current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    campaign = _campaign(db, campaign_id, current_user)
    rows = history_rows(db, campaign)
    return {"campaign_id": campaign.id, "items": rows, "total": len(rows)}


def public_audit_detail(value):
    """Creative reports do not duplicate the private source lyrics manifest."""
    if isinstance(value, dict):
        return {k: public_audit_detail(v) for k, v in value.items() if k != "source_reference"}
    if isinstance(value, list):
        return [public_audit_detail(v) for v in value]
    return value


@router.get("/{campaign_id}/creative/report")
def report(campaign_id: str, current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    campaign = _campaign(db, campaign_id, current_user)
    contract = _plan(campaign).get("contract") or {}
    videos = history_rows(db, campaign)
    delivered = {(r.detail["job_id"], r.detail["video_sha256"]) for r in _logs(db, campaign, "delivered").all()}
    items = {i.id: i for i in _items(db, campaign)}
    groups = []
    for index, g in enumerate(contract.get("groups", [])):
        ids = {i for i in contract.get("item_ids", []) if i in items and (items[i].render_overrides or {}).get(ASSIGNMENT, {}).get("group_id") == g["id"]}
        principal = [v for v in videos if v["item_id"] in ids and not v["parent_job_id"]]
        groups.append({"id": g["id"], "name": g["name"], "target": contract["counts"][index],
                       "target_weight": g["weight"], "assigned": len(ids),
                       "generated": sum(bool(v["video_url"]) for v in principal),
                       "approved": sum(v["status"] == "done" and bool(v["approved_at"]) for v in principal),
                       "verified": sum(v["compliance"] == "verified" for v in principal),
                       "delivered": sum((v["job_id"], v["evidence"].get("video_sha256")) in delivered for v in principal)})
    return {"campaign_id": campaign.id, "name": campaign.name, "at": now(), "contract": contract,
            "groups": groups, "videos": videos, "delivery_note": "La generación no acredita entrega al cliente. Registrar la entrega del archivo aprobado.",
            "assignments": [{"item_id": i.id, "artist": i.artist, "title": i.title or i.filename,
                             "assignment": (i.render_overrides or {}).get(ASSIGNMENT) or {},
                             "settings": effective_settings(campaign, i.render_overrides)} for i in items.values()],
            "history": [{"at": str(r.created_at), "actor": r.user_id, "action": r.action, "detail": public_audit_detail(r.detail)}
                        for r in db.query(AuditLog).filter(AuditLog.action.in_(["campaign.creative.applied", "campaign.creative.undone", "campaign.creative.individual", "campaign.creative.rendered", "campaign.creative.delivered"]),
                        AuditLog.detail["campaign_id"].as_string() == campaign.id).order_by(AuditLog.id).all()]}


@router.get("/{campaign_id}/creative/export.csv")
def export_csv(campaign_id: str, current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    data = report(campaign_id, current_user, db)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Campaña", "Fecha del informe", "Acuerdo", "Versión del acuerdo", "Universo", "Redondeo",
                     "Canción ID", "Video", "Artista", "Canción", "Grupo", "Estado", "Cumplimiento",
                     "Efecto aplicado", "Modelos", "SHA256 video", "Aprobación", "Configuración asignada", "Configuración enviada"])
    def safe(v):
        s = str(v if v is not None else "")
        return "'" + s if s.lstrip().startswith(("=", "+", "-", "@")) else s
    assignments = {a["item_id"]: a for a in data["assignments"]}
    entries = [(v, assignments.get(v["item_id"], {})) for v in data["videos"]]
    rendered_ids = {v["item_id"] for v in data["videos"]}
    entries += [({}, a) for a in data["assignments"] if a["item_id"] not in rendered_ids]
    for v, a in entries:
        evidence = v.get("evidence", {})
        writer.writerow([safe(x) for x in [data["name"], data["at"], data["contract"].get("agreement"),
            data["contract"].get("revision"), len(data["contract"].get("item_ids", [])), data["contract"].get("rounding_note"),
            a.get("item_id"), v.get("job_id"), v.get("artist", a.get("artist")), v.get("title", a.get("title")),
            v.get("assignment", a.get("assignment", {})).get("group_name"), v.get("status", "Sin generar"),
            v.get("compliance", "pending"), evidence.get("effect_applied"), ",".join(evidence.get("models", [])),
            evidence.get("video_sha256"), v.get("approved_at"), json.dumps(a.get("settings", {}), ensure_ascii=False),
            json.dumps(v.get("settings", {}), ensure_ascii=False)]])
    return Response("\ufeff" + output.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="campana-{campaign_id}.csv"'})


@router.get("/{campaign_id}/creative/export.xlsx")
def export_xlsx(campaign_id: str, current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    """Small OOXML workbook with inline strings: no formulas or extra runtime."""
    from zipfile import ZipFile, ZIP_DEFLATED
    from xml.sax.saxutils import escape
    data = report(campaign_id, current_user, db)
    # One changed field per row keeps even a 1,000-song operation complete;
    # a single JSON cell would silently exceed Excel's 32,767-character limit.
    changes = [["Fecha", "Responsable", "Acción", "Operación", "Motivo", "Canción", "Campo", "Antes", "Después"]]
    for h in data["history"]:
        detail = h["detail"]
        prefix = [h["at"], h["actor"], h["action"], detail.get("operation_id", ""), detail.get("reason", "")]
        records = detail.get("changes") or ([detail] if "item_id" in detail and "before" in detail else [])
        for c in records:
            before, after = c.get("before") or {}, c.get("after") or {}
            for k in sorted(set(before) | set(after)):
                if before.get(k) != after.get(k):
                    changes.append(prefix + [c.get("item_id"), k, json.dumps(before.get(k), ensure_ascii=False), json.dumps(after.get(k), ensure_ascii=False)])
        for k, value in detail.items():
            if k not in {"changes", "before", "after"} or not records:
                # Expanded nested values avoid truncating contracts with large
                # universes or render metadata. Paths preserve the structure.
                def expand(path, node):
                    if isinstance(node, dict):
                        for key, child in node.items():
                            yield from expand(f"{path}.{key}", child)
                    elif isinstance(node, list):
                        for index, child in enumerate(node):
                            yield from expand(f"{path}[{index}]", child)
                    else:
                        yield [path, node]
                changes.extend(prefix + [detail.get("job_id", ""), path, "", value] for path, value in expand(k, value))
    sheets = [
        ("Acuerdo", [["Campaña", data["name"]], ["Fecha", data["at"]], ["Acuerdo", data["contract"].get("agreement", "")],
                     ["Versión", data["contract"].get("revision", "")], ["Redondeo", data["contract"].get("rounding_note", "")],
                     ["Universo", len(data["contract"].get("item_ids", []))],
                     ["Grupo", "Cuota acordada", "Objetivo", "Asignadas", "Generadas", "Aprobadas", "Verificadas", "Entregadas"]]
                    + [[g[k] for k in ("name", "target_weight", "target", "assigned", "generated", "approved", "verified", "delivered")] for g in data["groups"]]),
        ("Videos", [["Video", "Artista", "Canción", "Grupo", "Estado", "Cumplimiento", "Configuración prevista", "Evidencia real"]]
                    + [[v["job_id"], v["artist"], v["title"], v["assignment"].get("group_name", ""), v["status"], v["compliance"],
                        json.dumps(v["settings"], ensure_ascii=False), json.dumps(v["evidence"], ensure_ascii=False)] for v in data["videos"]]),
        ("Cambios", changes),
        ("Asignaciones", [["Canción ID", "Artista", "Canción", "Grupo", "Versión", "Excepción fijada"] + list(FIELDS)]
            + [[a["item_id"], a["artist"], a["title"], a["assignment"].get("group_name", ""), a["assignment"].get("revision", ""), a["assignment"].get("pinned", False)]
               + [a["settings"].get(k, "") for k in FIELDS] for a in data["assignments"]]),
    ]
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/package/2006/relationships"
    docrel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    output = io.BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as z:
        overrides = ''.join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1, len(sheets) + 1))
        z.writestr("[Content_Types].xml", f'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>{overrides}</Types>')
        z.writestr("_rels/.rels", f'<Relationships xmlns="{rel}"><Relationship Id="rId1" Type="{docrel}/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr("xl/workbook.xml", f'<workbook xmlns="{ns}" xmlns:r="{docrel}"><sheets>' + ''.join(f'<sheet name="{name}" sheetId="{i}" r:id="rId{i}"/>' for i, (name, _) in enumerate(sheets, 1)) + '</sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", f'<Relationships xmlns="{rel}">' + ''.join(f'<Relationship Id="rId{i}" Type="{docrel}/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1, len(sheets) + 1)) + '</Relationships>')
        for i, (_, rows) in enumerate(sheets, 1):
            def cell(value):
                # Strip characters XML 1.0 cannot encode. Inline strings cannot
                # execute user-supplied spreadsheet formulas.
                text = ''.join(c for c in str(value if value is not None else "") if c in "\t\n\r" or 32 <= ord(c) <= 0xD7FF or 0xE000 <= ord(c) <= 0xFFFD or 0x10000 <= ord(c) <= 0x10FFFF)
                return '<c t="inlineStr"><is><t xml:space="preserve">' + escape(text[:32767]) + '</t></is></c>'
            z.writestr(f"xl/worksheets/sheet{i}.xml", f'<worksheet xmlns="{ns}"><sheetData>' + ''.join('<row>' + ''.join(cell(v) for v in row) + '</row>' for row in rows) + '</sheetData></worksheet>')
    return Response(output.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="campana-{campaign_id}.xlsx"'})
