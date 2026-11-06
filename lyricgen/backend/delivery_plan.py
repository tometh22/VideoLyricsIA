"""Delivery mix at campaign and portal-account level.

Planning never calls a provider or a queue. A preview records an exact diff;
apply rechecks the entire balance and locks local campaigns/jobs/items before
writing assignments in one transaction. Remote campaigns are read-only.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import uuid
from collections import Counter, defaultdict
from contextlib import ExitStack
from datetime import datetime, timezone
from typing import Literal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session, load_only

from auth import get_current_user
from batch_campaigns import _aware, _campaign_or_404, _require_manager, _require_scope
from campaign_creative import (
    ASSIGNMENT, KEY, Group, _plan, effective_settings,
    validate_combination,
)
from campaign_models import VEO_LITE
from delivery_freshness import render_fingerprint
from database import (
    AIProvenance, AuditLog, BatchCampaign, BatchCampaignItem, Delivery,
    EditorDocument, Job, get_db, get_deliveries_db, peer_session,
)

router = APIRouter(prefix="/batch/delivery-plan", tags=["delivery-plan"])
PORTALS = {"argentina": "Universal Argentina", "chile": "Universal Chile"}
TYPES = ("veo", "photo_effect", "unknown")
EDITABLE = {"transcribed_pending", "transcribed", "lyrics_approved", "transcribing",
            "transcribing_queued", "separation_ready", "separating", "separation_queued"}
JOB_FIELDS = (Job.job_id, Job.tenant_id, Job.campaign_id, Job.campaign_item_id,
              Job.parent_job_id, Job.artist, Job.song_title, Job.filename, Job.status,
              Job.video_url, Job.bg_r2_key_cached, Job.completed_at, Job.edit_count, Job.previous_versions)
RENDER_FIELDS = ("art_track", "campaign_render_evidence", "movement_style", "effect")


def _jobs(db, condition):
    # A job can contain megabytes of word-level transcript inside render_params.
    # A balance needs only four keys, never lyrics, prompts or previous files.
    fields = [((Job.render_params[k].as_boolean() if k == "art_track" else Job.render_params[k])).label(f"rp_{k}") for k in RENDER_FIELDS]
    records = db.query(*JOB_FIELDS, *fields).filter(condition).all()
    result = []
    for record in records:
        values = dict(record._mapping)
        values["render_params"] = {k: values.pop(f"rp_{k}") for k in RENDER_FIELDS}
        result.append(SimpleNamespace(**values))
    return result


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def check_period(period):
    if period:
        try:
            if not re.fullmatch(r"\d{4}-\d{2}", period):
                raise ValueError()
            datetime.strptime(period, "%Y-%m")
        except ValueError:
            raise HTTPException(422, "El período debe ser un mes válido (AAAA-MM)")
    return period or ""


def assignment_type(settings, assignment):
    """Only count intentional, compatible settings; auto is not a promise."""
    requirement = (assignment or {}).get("requirement")
    if requirement == "photo_effect":
        if (settings.get("movement_style") == "foto-parallax"
                and settings.get("effect") not in (None, "", "none", "foto_viva")
                and not settings.get("animate_image") and not settings.get("enable_scenes")
                and settings.get("background_mode") != "variation"):
            return "photo_effect"
    elif requirement == "veo":
        if (settings.get("movement_style") not in (None, "", "foto-parallax", "foto-estatica")
                and not settings.get("background_id") and settings.get("effect") != "foto_viva"):
            return "veo"
    return "unknown"


def rendered_type(job, calls=(), previews=None):
    if not job:
        return "unknown"
    params = job.render_params or {}
    if params.get("art_track") is True:
        return "art_track"
    evidence = params.get("campaign_render_evidence") or {}
    kind = evidence.get("background_kind")
    if kind == "image":
        return "photo_effect" if evidence.get("effect_applied") not in (None, "", "none", "foto_viva") else "unknown"
    if kind == "video":
        model = (evidence.get("background_origin") or {}).get("model") or ""
        return "veo" if "veo" in model.lower() else "unknown"
    # Legacy AI images and Veo clips BOTH set background_ai_generated=True.
    # Only successful generation evidence (including the exact preview key)
    # can establish the background type; a failed Veo call proves nothing.
    successful = []
    for call in calls:
        summary = call.response_summary or ""
        if summary.startswith(("video_generated:", "image_generated:")):
            successful.append(call)
        if call.tool_name == "bg-cache-reuse":
            match = re.search(r"key=([a-f0-9]+)", summary)
            successful.extend((previews or {}).get(match[1], []) if match else [])
    if successful:
        last = max(successful, key=lambda c: _aware(c.created_at))
        if "veo" in last.tool_name.lower():
            return "veo"
        if last.step == "image_bg" and params.get("movement_style") == "foto-parallax" and params.get("effect") not in (None, "", "none", "foto_viva"):
            return "photo_effect"
    return "unknown"


def balance(fixed, pending):
    """Nearest feasible 50/50 split; odd totals explicitly favor photo."""
    total = fixed.get("veo", 0) + fixed.get("photo_effect", 0) + pending
    desired_veo = total // 2
    veo = min(pending, max(0, desired_veo - fixed.get("veo", 0)))
    photo = pending - veo
    final_veo, final_photo = fixed.get("veo", 0) + veo, fixed.get("photo_effect", 0) + photo
    return {"veo": veo, "photo_effect": photo, "total": pending,
            "projected_veo": final_veo, "projected_photo_effect": final_photo,
            "remaining_gap": abs(final_veo - final_photo), "odd_total": bool(total % 2)}


def _authorize(db, user, campaign_id, portal_id, write=False):
    _require_scope(user)
    if bool(campaign_id) == bool(portal_id):
        raise HTTPException(422, "Elegí una campaña o una cuenta")
    if portal_id:
        if portal_id not in PORTALS:
            raise HTTPException(422, "Cuenta desconocida")
        if user.get("role") != "admin":
            raise HTTPException(403, "El saldo completo de la cuenta requiere un administrador")
        return None
    campaign = _campaign_or_404(db, campaign_id, user)
    if campaign.kind != "lyric_video":
        raise HTTPException(422, "Los Art Tracks tienen un conteo separado")
    if write:
        _require_manager(campaign, user)
    return campaign


def _campaign_period(c):
    return (c.default_render_params or {}).get("delivery_plan_period") or ""


def _discarded(item):
    return bool(item.discard_record and not item.discard_record.get("restored_at"))


def _source(db, campaign=None):
    campaigns = [campaign] if campaign else db.query(BatchCampaign).all()
    ids = [c.id for c in campaigns]
    items = db.query(BatchCampaignItem).filter(BatchCampaignItem.campaign_id.in_(ids)).all() if ids else []
    if campaign:
        from sqlalchemy import select, or_
        lineage = select(Job.job_id).where(Job.campaign_id == campaign.id, Job.tenant_id == campaign.tenant_id).cte(recursive=True)
        lineage = lineage.union(select(Job.job_id).join(lineage, Job.parent_job_id == lineage.c.job_id).where(
            Job.tenant_id == campaign.tenant_id, or_(Job.campaign_id.is_(None), Job.campaign_id == campaign.id)))
        jobs = _jobs(db, Job.job_id.in_(select(lineage.c.job_id)))
    else:
        from sqlalchemy import select, and_, or_
        lineage = select(Job.job_id, Job.tenant_id, Job.campaign_id.label("root_campaign")).where(Job.campaign_id.in_(ids)).cte(recursive=True)
        lineage = lineage.union(select(Job.job_id, Job.tenant_id, lineage.c.root_campaign).join(
            lineage, and_(Job.parent_job_id == lineage.c.job_id, Job.tenant_id == lineage.c.tenant_id)).where(
                or_(Job.campaign_id.is_(None), Job.campaign_id == lineage.c.root_campaign)))
        jobs = _jobs(db, Job.job_id.in_(select(lineage.c.job_id))) if ids else []
    return campaigns, items, jobs


def collect(db, ddb, user, campaign_id=None, portal_id=None, period="", peer=None):
    """One record per active publication, one pending record per uploaded item."""
    campaign = _authorize(db, user, campaign_id, portal_id)
    period = check_period(period)
    warnings, blockers = [], []
    sources = [("local", db, *_source(db, campaign))]
    if not campaign and peer is not None:
        sources.append(("peer", peer, *_source(peer)))
    publications = ddb.query(Delivery).filter(Delivery.removed_at.is_(None))
    if campaign:
        ids = [j.job_id for j in sources[0][4]]
        publications = publications.filter(Delivery.job_id.in_(ids), Delivery.tenant_snapshot == campaign.tenant_id)
    publications = publications.order_by(Delivery.id.desc()).all()
    # The portal is authoritative; staging's legacy delivery mirror is never added.
    publications = list({(d.job_id, d.portal_id or "argentina"): d for d in reversed(publications)}.values())
    publication_ids = {d.job_id for d in publications}
    jobs_by_key, calls, cache_calls, first_dates = {}, defaultdict(list), defaultdict(list), {}
    job_sources = {}
    for name, session, cs, items, jobs in sources:
        missing = publication_ids - {j.job_id for j in jobs}
        if missing:
            jobs += _jobs(session, Job.job_id.in_(missing))
        for j in jobs:
            jobs_by_key.setdefault((j.job_id, j.tenant_id), j)
            job_sources.setdefault((j.job_id, j.tenant_id), name)
        ids = list({j.job_id for j in jobs})
        provenance_fields = (AIProvenance.job_id, AIProvenance.step, AIProvenance.tool_name, AIProvenance.response_summary, AIProvenance.created_at)
        records = session.query(AIProvenance).options(load_only(*provenance_fields)).filter(AIProvenance.job_id.in_(ids), AIProvenance.step.in_(["video_bg", "image_bg"])).all() if ids else []
        cache_keys = set()
        for call in records:
            calls[call.job_id].append(call)
            match = re.search(r"key=([a-f0-9]+)", call.response_summary or "") if call.tool_name == "bg-cache-reuse" else None
            if match:
                cache_keys.add(match[1])
        if cache_keys:
            previews = session.query(Job.job_id, Job.filename).filter(Job.filename.in_([f"bgpreview_{k}.preview" for k in cache_keys])).all()
            mapping = {j.job_id: j.filename.removeprefix("bgpreview_").removesuffix(".preview") for j in previews}
            for call in session.query(AIProvenance).options(load_only(*provenance_fields)).filter(AIProvenance.job_id.in_(list(mapping))).all() if mapping else []:
                if (call.response_summary or "").startswith(("video_generated:", "image_generated:")):
                    cache_calls[mapping[call.job_id]].append(call)
        if period and publication_ids:
            for audit in session.query(AuditLog).filter(AuditLog.action.in_(["delivery.create", "delivery.update"]), AuditLog.detail["job_id"].as_string().in_(list(publication_ids))).all():
                info = audit.detail or {}
                key = (info.get("job_id"), info.get("portal_id") or "argentina")
                first_dates[key] = min(first_dates.get(key, _aware(audit.created_at)), _aware(audit.created_at))

    buckets = {stage: Counter({kind: 0 for kind in TYPES}) for stage in ("delivered", "generated", "pending", "in_progress")}
    rows, signatures, issues, video_rows = [], [], [], []
    excluded_art = 0
    delivered_keys = set()
    for d in publications:
        country = d.portal_id or "argentina"
        if portal_id and country != portal_id:
            continue
        key = (d.job_id, d.tenant_snapshot)
        delivered_keys.add(key)
        first = min(first_dates.get((d.job_id, country), _aware(d.added_at)), _aware(d.added_at))
        if period:
            tz = ZoneInfo("America/Santiago" if country == "chile" else "America/Argentina/Buenos_Aires")
            if first.astimezone(tz).strftime("%Y-%m") != period:
                continue
        job = jobs_by_key.get(key)
        kind = rendered_type(job, calls[d.job_id], cache_calls)
        outdated = bool(job and d.published_render_fingerprint and d.published_render_fingerprint != render_fingerprint(job))
        if outdated and kind != "art_track":
            kind = "unknown"
        if kind == "unknown":
            issues.append({"job_id": d.job_id, "source": job_sources.get(key, "missing"), "title": d.song_title_snapshot, "campaign_id": job.campaign_id if job else None,
                           "reason": "La versión publicada difiere del render actual" if outdated else "Falta evidencia del fondo publicado"})
        if kind == "art_track":
            excluded_art += 1
        else:
            buckets["delivered"][kind] += 1
            video_rows.append({"item_id": f"delivery:{d.job_id}:{country}", "source": job_sources.get(key, "missing"), "campaign_id": job.campaign_id if job else None,
                               "campaign_name": "", "title": d.song_title_snapshot, "artist": d.artist_snapshot,
                               "job_id": d.job_id, "status": job.status if job else "done", "type": kind,
                               "effect": ((job.render_params or {}).get("campaign_render_evidence") or {}).get("effect_applied", "") if job else "",
                               "stage": "delivered", "editable": False, "ready": False, "pinned": False,
                               "blocked_reason": "Entregado · versión a verificar" if outdated else "Entregado al portal"})
        signatures.append(("delivery", d.id, d.job_id, d.added_at, d.published_revision, kind, d.stale_since))

    campaign_summaries, unmapped, seen_campaigns = [], [], set()
    now = datetime.now(timezone.utc)
    for source, session, cs, items, jobs in sources:
        jobs_for_items = {j.campaign_item_id: j for j in jobs if j.campaign_item_id}
        item_map = {i.id: i for i in items}
        docs = session.query(EditorDocument.job_id, EditorDocument.lock_expires_at).filter(EditorDocument.job_id.in_([j.job_id for j in jobs])).all() if jobs else []
        locked = {d.job_id for d in docs if d.lock_expires_at and _aware(d.lock_expires_at) > now}
        for c in cs:
            if c.id in seen_campaigns:
                continue
            seen_campaigns.add(c.id)
            if c.status == "cancelled" or c.kind != "lyric_video":
                continue
            cjobs = [j for j in jobs if j.campaign_id == c.id or (campaign and j.tenant_id == c.tenant_id)]
            owned = {j.job_id for j in cjobs}
            while True:
                children = [j for j in jobs if j.job_id not in owned and j.tenant_id == c.tenant_id and not j.campaign_id and j.parent_job_id in owned]
                if not children:
                    break
                cjobs.extend(children)
                owned.update(j.job_id for j in children)
            # An explicit portal wins. A unique historical destination is a
            # read-only hint for legacy campaigns; never infer from tenant/name.
            destinations = {d.portal_id or "argentina" for d in publications if d.job_id in {j.job_id for j in cjobs} and d.tenant_snapshot == c.tenant_id}
            account = c.destination_portal or (next(iter(destinations)) if len(destinations) == 1 else None)
            if not campaign and not account:
                unmapped.append({"id": c.id, "name": c.name, "reason": "account"})
                continue
            if portal_id and account != portal_id:
                continue
            if period and not campaign and _campaign_period(c) != period:
                if not _campaign_period(c):
                    unmapped.append({"id": c.id, "name": c.name, "reason": "period"})
                continue
            writable = source == "local" and (user.get("role") == "admin" or c.created_by == user.get("id"))
            pending_count, ready_count = 0, 0
            signatures.append((source, c.id, c.status, c.destination_portal, c.default_render_params))
            generated_item_ids = set()
            for j in cjobs:
                if j.status == "discarded" or (j.campaign_item_id in item_map and _discarded(item_map[j.campaign_item_id])):
                    continue
                evidence = (j.render_params or {}).get("campaign_render_evidence") or {}
                if not (j.video_url or evidence.get("video_sha256")):
                    continue
                if j.campaign_item_id:
                    generated_item_ids.add(j.campaign_item_id)
                if (j.job_id, j.tenant_id) in delivered_keys:
                    continue
                kind = rendered_type(j, calls[j.job_id], cache_calls)
                if kind == "art_track":
                    continue
                buckets["generated"][kind] += 1
                video_rows.append({"item_id": f"generated:{source}:{j.job_id}", "source": source, "campaign_id": c.id,
                                   "campaign_name": c.name, "title": j.song_title or j.filename, "artist": j.artist,
                                   "job_id": j.job_id, "status": j.status, "type": kind, "effect": evidence.get("effect_applied", ""),
                                   "stage": "generated", "editable": False, "ready": False, "pinned": False,
                                   "blocked_reason": "Generado, pendiente de entregar"})
                if kind == "unknown":
                    issues.append({"job_id": j.job_id, "stage": "generated", "source": source, "title": j.song_title or j.filename, "campaign_id": c.id,
                                   "reason": "Falta evidencia del fondo generado"})
                signatures.append((source, j.job_id, j.status, j.completed_at, evidence, kind))
            for i in items:
                if i.campaign_id != c.id or i.upload_state != "uploaded" or _discarded(i):
                    continue
                j = jobs_for_items.get(i.id)
                if i.id in generated_item_ids or (j and (j.status == "discarded" or (j.job_id, j.tenant_id) in delivered_keys)):
                    continue
                settings = effective_settings(c, i.render_overrides)
                assignment = (i.render_overrides or {}).get(ASSIGNMENT) or {}
                kind = assignment_type(settings, assignment)
                inflight = bool(j and j.status not in EDITABLE and j.status not in ("error", "failed"))
                stage = "in_progress" if inflight else "pending"
                buckets[stage][kind] += 1
                editable = writable and c.status == "active" and (not j or j.status in EDITABLE) and (not j or j.job_id not in locked)
                row = {"item_id": i.id, "campaign_id": c.id, "campaign_name": c.name,
                       "portal_id": account, "title": i.title or i.filename, "artist": i.artist or "",
                       "job_id": j.job_id if j else None, "status": j.status if j else "waiting",
                       "type": kind, "effect": settings.get("effect") or "", "stage": stage,
                       "settings": settings, "assignment": assignment,
                       "pinned": bool(assignment.get("pinned")), "editable": editable,
                       "revision": _plan(c)["revision"],
                       "ready": bool(editable and j and j.status == "lyrics_approved" and kind != "unknown"),
                       "blocked_reason": "Otro entorno" if not writable else "Campaña pausada o completada" if c.status != "active" else "Abierta en el editor" if j and j.job_id in locked else "En generación" if inflight else "Requiere atención" if not editable else ""}
                rows.append(row)
                pending_count += stage == "pending"
                ready_count += row["ready"]
                signatures.append((source, i.id, i.upload_state, i.discard_record, i.render_overrides, j.job_id if j else None, j.status if j else None, j.job_id in locked if j else False))
            campaign_summaries.append({"id": c.id, "name": c.name, "portal_id": account,
                                       "period": _campaign_period(c), "pending": pending_count,
                                       "ready": ready_count, "writable": writable})
    if unmapped:
        warnings.append("Hay campañas sin cuenta o período asociado. No se incluyen en este saldo de cuenta.")
    fixed = buckets["delivered"] + buckets["generated"] + buckets["in_progress"]
    candidates = [r for r in rows if r["stage"] == "pending" and r["editable"] and not r["pinned"]]
    candidate_ids = {r["item_id"] for r in candidates}
    for r in rows:
        if r["stage"] == "pending" and r["item_id"] not in candidate_ids:
            fixed[r["type"]] += 1
    if fixed["unknown"]:
        blockers.append(f"Hay {fixed['unknown']} fondos sin evidencia o asignación verificable fuera de las canciones disponibles. Revisalos antes de equilibrar.")
    if not candidates:
        blockers.append("No hay canciones pendientes disponibles para repartir.")
    if campaign and not (user.get("role") == "admin" or campaign.created_by == user.get("id")):
        blockers.append("Sólo el responsable de la campaña puede guardar el reparto.")
    proposal = balance(fixed, len(candidates))
    projected = buckets["delivered"] + buckets["generated"] + buckets["in_progress"] + buckets["pending"]
    scope = f"campaign:{campaign_id}" if campaign else f"portal:{portal_id}"
    environment = os.environ.get("RAILWAY_ENVIRONMENT_NAME", os.environ.get("ENVIRONMENT", "")).lower()
    peer_frontend_url = {"production": "https://staging.genly.pro", "prod": "https://staging.genly.pro", "staging": "https://genly.pro"}.get(environment)
    result = {"scope": scope, "name": campaign.name if campaign else PORTALS[portal_id], "peer_frontend_url": peer_frontend_url,
              "portal_id": (campaign.destination_portal if campaign else portal_id),
              "period": period, "campaign_period": _campaign_period(campaign) if campaign else None,
              "target": {"veo": 50, "photo_effect": 50}, "buckets": {k: dict(v) for k, v in buckets.items()},
              "projected": {k: projected[k] for k in TYPES}, "suggestion": proposal,
              "excluded_art_tracks": excluded_art, "warnings": warnings, "blockers": blockers,
              "unmapped_campaigns": unmapped, "campaigns": campaign_summaries, "items": rows + video_rows, "issues": issues,
              "can_manage": bool(user.get("role") == "admin" or (campaign and campaign.created_by == user.get("id"))),
              "can_view_accounts": user.get("role") == "admin",
              "fingerprint": digest([scope, period, sorted(signatures, key=digest), sorted(rows, key=lambda r: r["item_id"]), proposal]),
              "date_basis": "Primera publicación registrada; pendientes por período asignado a la campaña."}
    return result


def _collect(db, ddb, user, **scope):
    _authorize(db, user, scope.get("campaign_id"), scope.get("portal_id"))
    with ExitStack() as stack:
        # A peer read failure must never turn a partial account into a
        # plausible zero. Let the request fail; no assignments can be applied.
        peer = peer_session() if not scope.get("campaign_id") else None
        if peer is not None:
            stack.callback(peer.close)
        result = collect(db, ddb, user, peer=peer, **scope)
        if not scope.get("campaign_id") and peer is None:
            result["warnings"].append("No está conectado el otro entorno: el saldo incluye las publicaciones del portal y las campañas de este entorno. Puede faltar producción.")
            result["blockers"].append("Conectá el otro entorno antes de equilibrar el saldo completo de la cuenta. Podés asignar fondos por campaña.")
        return result


@router.get("")
def get_plan(campaign_id: str | None = None, portal_id: str | None = None,
             period: str = "", current_user=Depends(get_current_user),
             db: Session = Depends(get_db), ddb: Session = Depends(get_deliveries_db)):
    return _collect(db, ddb, current_user, campaign_id=campaign_id, portal_id=portal_id, period=period)


class Scope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    campaign_id: str | None = None
    portal_id: Literal["argentina", "chile"] | None = None
    period: str = ""


class PreviewRequest(Scope):
    fingerprint: str


class ApplyRequest(BaseModel):
    preview_id: str


def _settings(kind):
    return {"movement_style": "foto-parallax" if kind == "photo_effect" else "estandar",
            "effect": "bokeh" if kind == "photo_effect" else "", "background_id": None,
            "background_mode": "as_is", "animate_image": False, "enable_scenes": False}


@router.post("/preview")
def preview(body: PreviewRequest, current_user=Depends(get_current_user),
            db: Session = Depends(get_db), ddb: Session = Depends(get_deliveries_db)):
    scope = body.model_dump(exclude={"fingerprint"})
    _authorize(db, current_user, body.campaign_id, body.portal_id, True)
    data = _collect(db, ddb, current_user, **scope)
    if data["fingerprint"] != body.fingerprint:
        raise HTTPException(409, "El saldo cambió. Actualizá antes de repartir.")
    if data["blockers"]:
        raise HTTPException(409, " ".join(data["blockers"]))
    candidates = sorted((r for r in data["items"] if r["stage"] == "pending" and r["editable"] and not r["pinned"]), key=lambda r: (r["campaign_id"], r["item_id"]))
    # Retain existing choices where possible; unassigned songs absorb the gap.
    count = data["suggestion"]["veo"]
    order = sorted(candidates, key=lambda r: ({"veo": 0, "unknown": 1, "photo_effect": 2}[r["type"]], r["campaign_id"], r["item_id"]))
    veo_ids = {r["item_id"] for r in order[:count]}
    changes = [{**r, "after_type": "veo" if r["item_id"] in veo_ids else "photo_effect"} for r in candidates]
    detail = {"preview_id": str(uuid.uuid4()), "scope": scope, "fingerprint": data["fingerprint"],
              "changes": changes, "suggestion": data["suggestion"]}
    db.add(AuditLog(user_id=current_user["id"], action="delivery_plan.preview", detail=detail))
    db.commit()
    return detail


@router.post("/apply")
def apply(body: ApplyRequest, current_user=Depends(get_current_user),
          db: Session = Depends(get_db), ddb: Session = Depends(get_deliveries_db)):
    record = db.query(AuditLog).filter(AuditLog.action == "delivery_plan.preview", AuditLog.user_id == current_user["id"], AuditLog.detail["preview_id"].as_string() == body.preview_id).first()
    if not record:
        raise HTTPException(404, "Vista previa no encontrada")
    detail = record.detail
    scope = detail["scope"]
    _authorize(db, current_user, scope.get("campaign_id"), scope.get("portal_id"), True)
    campaign_ids = sorted({r["campaign_id"] for r in detail["changes"]})
    campaigns = db.query(BatchCampaign).filter(BatchCampaign.id.in_(campaign_ids)).order_by(BatchCampaign.id).populate_existing().with_for_update().all()
    previous = db.query(AuditLog).filter(AuditLog.action == "delivery_plan.applied", AuditLog.detail["preview_id"].as_string() == body.preview_id).first()
    if previous:
        return {"ok": True, "deduplicated": True}
    db.query(Job).filter(Job.campaign_id.in_(campaign_ids)).order_by(Job.job_id).populate_existing().with_for_update().all()
    items = {i.id: i for i in db.query(BatchCampaignItem).filter(BatchCampaignItem.campaign_id.in_(campaign_ids)).order_by(BatchCampaignItem.id).populate_existing().with_for_update().all()}
    db.query(EditorDocument).filter(EditorDocument.job_id.in_(db.query(Job.job_id).filter(Job.campaign_id.in_(campaign_ids)))).order_by(EditorDocument.job_id).populate_existing().with_for_update().all()
    if (datetime.now(timezone.utc) - _aware(record.created_at)).total_seconds() > 1800:
        raise HTTPException(409, "La vista previa venció. Volvé a calcularla.")
    data = _collect(db, ddb, current_user, **scope)
    if data["fingerprint"] != detail["fingerprint"] or data["blockers"]:
        raise HTTPException(409, "Cambió el saldo o una canción. Revisá una nueva propuesta.")
    try:
        for c in campaigns:
            _require_manager(c, current_user)
            plan = copy.deepcopy(_plan(c))
            plan["revision"] += 1
            before, changes = copy.deepcopy(_plan(c)), []
            for row in detail["changes"]:
                if row["campaign_id"] != c.id:
                    continue
                i = items[row["item_id"]]
                kind = row["after_type"]
                patch = _settings(kind)
                # Keep a compatible chosen overlay instead of replacing it.
                old_settings = effective_settings(c, i.render_overrides)
                if kind == "photo_effect" and old_settings.get("effect") not in (None, "", "none", "foto_viva"):
                    patch["effect"] = old_settings["effect"]
                group = Group(id=f"plan-{kind}", name="Veo" if kind == "veo" else "Foto fija + efecto", weight=100,
                              requirement=kind, model=VEO_LITE if kind == "veo" else "", settings=patch)
                validate_combination(db, c, effective_settings(c, {**(i.render_overrides or {}), **patch}), group, current_user)
                old = copy.deepcopy(i.render_overrides or {})
                i.render_overrides = {**old, **patch, ASSIGNMENT: {"group_id": group.id, "group_name": group.name, "requirement": kind,
                    "model": group.model, "revision": plan["revision"], "pinned": False}}
                changes.append({"item_id": i.id, "before": old, "after": i.render_overrides})
            c.default_render_params = {**(c.default_render_params or {}), KEY: plan,
                                       "delivery_plan_target": {"veo": 50, "photo_effect": 50}}
            db.add(AuditLog(user_id=current_user["id"], action="campaign.creative.applied", detail={
                "campaign_id": c.id, "operation_id": body.preview_id, "revision": plan["revision"],
                "before_plan": before, "after_plan": plan, "changes": changes,
                "reason": "Equilibrar el plan de entrega 50/50"}))
        db.add(AuditLog(user_id=current_user["id"], action="delivery_plan.applied", detail=detail))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"ok": True, "assigned": len(detail["changes"])}


class Association(BaseModel):
    portal_id: Literal["argentina", "chile"]
    period: str = ""


@router.put("/campaigns/{campaign_id}/account")
def associate(campaign_id: str, body: Association, current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    campaign = _authorize(db, current_user, campaign_id, None, True)
    campaign = db.query(BatchCampaign).filter_by(id=campaign.id).populate_existing().with_for_update().one()
    if campaign.destination_portal and campaign.destination_portal != body.portal_id:
        raise HTTPException(409, "La campaña ya tiene otra cuenta asociada")
    period = check_period(body.period)
    campaign.destination_portal = body.portal_id
    campaign.default_render_params = {**(campaign.default_render_params or {}), "delivery_plan_period": period}
    db.add(AuditLog(user_id=current_user["id"], action="delivery_plan.account", detail={"campaign_id": campaign_id, **body.model_dump()}))
    db.commit()
    return {"ok": True}


class ItemChoice(BaseModel):
    revision: int = Field(ge=0)
    type: Literal["veo", "photo_effect"]
    pinned: bool = True


@router.put("/campaigns/{campaign_id}/items/{item_id}")
def choose_item(campaign_id: str, item_id: str, body: ItemChoice,
                current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    """A reviewed per-song exception, using the existing guarded bulk path."""
    from campaign_creative import PlanRequest, CommitRequest, preview as creative_preview, apply as creative_apply
    c = _authorize(db, current_user, campaign_id, None, True)
    item = db.query(BatchCampaignItem).filter_by(id=item_id, campaign_id=c.id).first()
    if not item or item.upload_state != "uploaded" or c.status != "active":
        raise HTTPException(409, "La canción no está disponible para asignar")
    patch = _settings(body.type)
    old = effective_settings(c, item.render_overrides)
    if body.type == "photo_effect" and old.get("effect") not in (None, "", "none", "foto_viva"):
        patch["effect"] = old["effect"]
    request = PlanRequest(revision=body.revision, item_ids=[item_id], replace_exceptions=True,
                          pin=body.pinned, reason="Elección individual desde el plan de entrega",
                          groups=[Group(id=f"plan-{body.type}", name="Veo" if body.type == "veo" else "Foto fija + efecto",
                                        weight=100, requirement=body.type, model=VEO_LITE if body.type == "veo" else "", settings=patch)])
    proposed = creative_preview(campaign_id, request, current_user, db)
    return creative_apply(campaign_id, CommitRequest(preview_id=proposed["preview_id"]), current_user, db)
