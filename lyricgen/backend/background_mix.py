"""Mix de fondos de lo publicado en los portales de UMG.

El contrato con Universal pide que la mitad de los video lyrics lleve fondo
de video generado con Veo Lite y la otra mitad foto fija + efecto. Esto
cuenta lo que el cliente TIENE en los portales (entregas vigentes, sin art
tracks), agrupado por campaña, para que el operador tilde qué campañas entran
en la cuenta del contrato.

De dónde sale cada dato:

* Las entregas viven en la base del portal (``scoped_deliveries_db``: la de
  producción, también desde staging).
* Los jobs están repartidos: la producción gestionada corre en staging y lo
  viejo de agosto en producción. Se busca primero en la base local y lo que
  falta en la del otro entorno (``scoped_peer_db``).
* El tipo de fondo NO sale del ``movement_style`` crudo: "foto-estatica" se
  normaliza a "estatico" y termina en Veo. Manda lo que el pipeline hizo de
  verdad (``ai_provenance``: ``image_bg`` = foto, ``video_bg`` = Veo), salvo
  "foto-parallax", que el pipeline siempre manda a foto aunque el job haya
  tenido un Veo en un render anterior.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from database import AIProvenance, BatchCampaign, Delivery, Job

ART_TRACK_LABEL = "art track"
NO_CAMPAIGN_PREFIX = "sin_campana:"
_BG_STEPS = ("video_bg", "image_bg")
_REUSE_TOOL = "bg-cache-reuse"
_CHUNK = 500
# Valores de movement_style que pipeline._normalize_movement_style lleva a
# "foto-parallax" (el único que el pipeline fuerza a foto). "foto-estatica"
# NO está: se normaliza a "estatico" y va a Veo. Un test fija la paridad.
PHOTO_MOVEMENTS = frozenset({"foto-parallax", "photo", "parallax", "foto+parallax", "foto_parallax"})


def _iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _chunks(values: list, size: int = _CHUNK) -> Iterable[list]:
    for i in range(0, len(values), size):
        yield values[i:i + size]


def veo_model(tool_name: str) -> str:
    name = (tool_name or "").lower()
    if name == _REUSE_TOOL:
        return "reuso"
    if "lite" in name:
        return "lite"
    if "fast" in name:
        return "fast"
    return "otro"


def classify(render_params: Optional[dict], provenance: list[tuple]) -> dict:
    """Tipo de fondo de UN job.

    ``provenance``: filas ``(step, tool_name, created_at)`` de ``video_bg`` /
    ``image_bg`` del job, en cualquier orden. Devuelve ``{"type", "model"}``
    con type ``veo`` | ``foto`` | ``sin_dato`` y model sólo para Veo.
    """
    params = render_params or {}
    movement = str(params.get("movement_style") or "").strip().lower()
    if movement in PHOTO_MOVEMENTS:
        return {"type": "foto", "model": None}
    rows = sorted(provenance, key=lambda r: r[2] or datetime.min.replace(tzinfo=timezone.utc))
    generated = [r for r in rows if (r[1] or "").lower() != _REUSE_TOOL]
    if generated:
        step, tool, _ = generated[-1]
        if step == "image_bg":
            return {"type": "foto", "model": None}
        return {"type": "veo", "model": veo_model(tool)}
    if rows:
        # Sólo reuso de un clip ya generado: el fondo es un video.
        return {"type": "veo", "model": "reuso"}
    return {"type": "sin_dato", "model": None}


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _load_jobs(session: Session, job_ids: list[str]) -> dict[str, dict]:
    jobs: dict[str, dict] = {}
    for chunk in _chunks(job_ids):
        for job_id, campaign_id, render_params in session.query(
            Job.job_id, Job.campaign_id, Job.render_params,
        ).filter(Job.job_id.in_(chunk)):
            jobs[job_id] = {"campaign_id": campaign_id, "render_params": render_params or {}, "prov": []}
    found = list(jobs)
    for chunk in _chunks(found):
        for job_id, step, tool, created_at in session.query(
            AIProvenance.job_id, AIProvenance.step, AIProvenance.tool_name, AIProvenance.created_at,
        ).filter(AIProvenance.job_id.in_(chunk), AIProvenance.step.in_(_BG_STEPS)):
            jobs[job_id]["prov"].append((step, tool, _aware(created_at)))
    campaign_ids = sorted({j["campaign_id"] for j in jobs.values() if j["campaign_id"]})
    campaigns = {}
    for chunk in _chunks(campaign_ids):
        for c in session.query(BatchCampaign).filter(BatchCampaign.id.in_(chunk)):
            campaigns[c.id] = c
    for j in jobs.values():
        j["campaign"] = campaigns.get(j["campaign_id"])
    return jobs


def _empty_group(key: str, **meta) -> dict:
    return {
        "key": key, **meta,
        "videos": 0, "veo": 0, "foto": 0, "sin_dato": 0,
        "veo_models": defaultdict(int), "efectos": defaultdict(int),
        "first_delivery_at": None, "last_delivery_at": None,
    }


def build_mix(deliveries_db: Session, local_db: Session, peer_db: Optional[Session]) -> dict:
    # Primera entrega vigente de cada job (mismo criterio que las métricas de
    # Finanzas): un job publicado dos veces es UN video.
    first: dict[str, Delivery] = {}
    for d in deliveries_db.query(Delivery).filter(Delivery.removed_at.is_(None)):
        if (d.label or "").strip().lower() == ART_TRACK_LABEL:
            continue
        current = first.get(d.job_id)
        if current is None or _aware(d.added_at) < _aware(current.added_at):
            first[d.job_id] = d

    job_ids = sorted(first)
    jobs = _load_jobs(local_db, job_ids)
    from_peer = 0
    if peer_db is not None:
        missing = [j for j in job_ids if j not in jobs]
        if missing:
            peer_jobs = _load_jobs(peer_db, missing)
            from_peer = len(peer_jobs)
            jobs.update(peer_jobs)

    groups: dict[str, dict] = {}
    items = []
    excluded_art_tracks = 0
    for job_id in job_ids:
        d = first[job_id]
        job = jobs.get(job_id)
        params = (job or {}).get("render_params") or {}
        campaign = (job or {}).get("campaign")
        if params.get("art_track") or (campaign is not None and campaign.kind == "art_track"):
            # Art track publicado con otra etiqueta ("Opción 3"): no es un
            # video lyric y no entra en el mix.
            excluded_art_tracks += 1
            continue
        portal = d.portal_id or "argentina"
        if campaign is not None:
            key = f"campaign:{campaign.id}"
            if key not in groups:
                groups[key] = _empty_group(
                    key, campaign_id=campaign.id, name=campaign.name,
                    portal=campaign.destination_portal or portal,
                    tenant=campaign.tenant_id, created_at=_iso(campaign.created_at),
                )
        else:
            key = f"{NO_CAMPAIGN_PREFIX}{portal}"
            if key not in groups:
                groups[key] = _empty_group(
                    key, campaign_id=None, name="Entregas sin campaña", portal=portal,
                    tenant=None, created_at=None,
                )
        group = groups[key]
        result = classify(params, job["prov"]) if job else {"type": "sin_dato", "model": None}
        effect = (params.get("effect") or "").strip() or None
        group["videos"] += 1
        group[result["type"]] += 1
        if result["type"] == "veo":
            group["veo_models"][result["model"]] += 1
        elif result["type"] == "foto":
            group["efectos"][effect or "sin efecto"] += 1
        added = _iso(d.added_at)
        if group["first_delivery_at"] is None or added < group["first_delivery_at"]:
            group["first_delivery_at"] = added
        if group["last_delivery_at"] is None or added > group["last_delivery_at"]:
            group["last_delivery_at"] = added
        items.append({
            "job_id": job_id, "group": key, "portal": portal,
            "artist": d.artist_snapshot, "song": d.song_title_snapshot, "label": d.label,
            "type": result["type"], "veo_model": result["model"], "effect": effect,
            "added_at": added, "found": job is not None,
        })

    out_groups = []
    for g in groups.values():
        g["veo_models"] = dict(g["veo_models"])
        g["efectos"] = dict(g["efectos"])
        out_groups.append(g)
    # Campañas primero, por fecha; las sin campaña al final.
    out_groups.sort(key=lambda g: (g["campaign_id"] is None, g["first_delivery_at"] or "", g["key"]))
    items.sort(key=lambda i: (i["added_at"] or "", i["job_id"]))
    not_found = sum(1 for i in items if not i["found"])
    return {
        "groups": out_groups,
        "items": items,
        "excluded_art_tracks": excluded_art_tracks,
        "jobs_from_peer": from_peer,
        "jobs_not_found": not_found,
        "single_environment": peer_db is None,
    }
