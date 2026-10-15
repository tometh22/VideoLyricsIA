"""Finanzas del admin: planilla editable + métricas de operación (super-admin).

Dos fuentes, a propósito separadas:

* ``finance_entries`` — lo que el sistema no puede saber solo: facturas
  verificadas, mano de obra pagada o imputada, estructura, cobros, términos
  de cada contrato y parámetros del reparto. Se edita desde la UI.
* ``/admin/finance/operations`` — lo que el sistema SÍ sabe: piezas
  entregadas por mes y portal (de ``deliveries``), material recibido (de las
  campañas), revisores, pedidos de cambio y tiempo de entrega. El ingreso
  devengado se calcula en el front con esto × el precio del contrato.

Los montos no se versionan nunca (el repo es público); llegan por la UI o
por ``/admin/finance/entries/import``.
"""
from __future__ import annotations

import logging
import re
import statistics
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from admin import require_super_admin
from database import (
    AuditLog,
    BatchCampaign,
    BatchCampaignItem,
    CostDaily,
    Delivery,
    DeliveryChangeRequest,
    FinanceEntry,
    Job,
    User,
    get_db,
    get_deliveries_db,
    utcnow,
)

logger = logging.getLogger("genly.finance")

router = APIRouter(prefix="/admin/finance", tags=["admin-finance"])

ART = ZoneInfo("America/Argentina/Buenos_Aires")
PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
SECTIONS = {
    "ingreso", "costo_directo", "mano_obra", "estructura", "cobro",
    "pago_socio", "contrato", "parametros",
}
BASES = {"factura", "api", "captura", "estimado", "imputado", "acuerdo", "sistema"}
ART_TRACK_LABEL = "art track"
MAX_IMPORT = 500


class EntryIn(BaseModel):
    period: str
    section: str
    category: Optional[str] = Field(default=None, max_length=40)
    provider: Optional[str] = Field(default=None, max_length=80)
    concept: Optional[str] = Field(default=None, max_length=200)
    amount_usd: Optional[float] = None
    basis: Optional[str] = None
    contract: Optional[str] = Field(default=None, max_length=80)
    evidence: Optional[str] = Field(default=None, max_length=4000)
    note: Optional[str] = Field(default=None, max_length=4000)
    payload: Optional[dict[str, Any]] = None

    @field_validator("period")
    @classmethod
    def _period(cls, value: str) -> str:
        value = (value or "").strip()
        if not PERIOD_RE.match(value):
            raise ValueError("period debe ser YYYY-MM")
        return value

    @field_validator("section")
    @classmethod
    def _section(cls, value: str) -> str:
        value = (value or "").strip()
        if value not in SECTIONS:
            raise ValueError(f"section inválida: {value}")
        return value

    @field_validator("basis")
    @classmethod
    def _basis(cls, value: Optional[str]) -> Optional[str]:
        if value in (None, ""):
            return None
        if value not in BASES:
            raise ValueError(f"basis inválida: {value}")
        return value

    @field_validator("amount_usd")
    @classmethod
    def _amount(cls, value: Optional[float]) -> Optional[float]:
        if value is None:
            return None
        if abs(value) > 10_000_000:
            raise ValueError("monto fuera de rango")
        return round(float(value), 2)


class EntryPatch(BaseModel):
    period: Optional[str] = None
    section: Optional[str] = None
    category: Optional[str] = Field(default=None, max_length=40)
    provider: Optional[str] = Field(default=None, max_length=80)
    concept: Optional[str] = Field(default=None, max_length=200)
    amount_usd: Optional[float] = None
    basis: Optional[str] = None
    contract: Optional[str] = Field(default=None, max_length=80)
    evidence: Optional[str] = Field(default=None, max_length=4000)
    note: Optional[str] = Field(default=None, max_length=4000)
    payload: Optional[dict[str, Any]] = None


class ImportIn(BaseModel):
    entries: list[EntryIn]
    # Reemplaza los renglones de esos (period, section) en vez de sumarse:
    # reimportar la misma planilla no duplica.
    replace: bool = False


def _to_dict(entry: FinanceEntry) -> dict:
    return {
        "id": entry.id,
        "period": entry.period,
        "section": entry.section,
        "category": entry.category,
        "provider": entry.provider,
        "concept": entry.concept,
        "amount_usd": entry.amount_usd,
        "basis": entry.basis,
        "contract": entry.contract,
        "evidence": entry.evidence,
        "note": entry.note,
        "payload": entry.payload,
        "updated_at": entry.updated_at.isoformat() if entry.updated_at else None,
    }


def _audit(db: Session, admin: dict, action: str, entry: FinanceEntry | None = None, **extra) -> None:
    # Sin montos en el audit: el log lo ven más roles que esta planilla.
    detail = {"entry_id": entry.id if entry else None, **extra}
    if entry is not None:
        detail.update({"period": entry.period, "section": entry.section})
    db.add(AuditLog(user_id=admin.get("id"), action=action, detail=detail))


@router.get("/entries")
def list_entries(
    admin: dict = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(FinanceEntry)
        .filter(FinanceEntry.deleted_at.is_(None))
        .order_by(FinanceEntry.period, FinanceEntry.section, FinanceEntry.id)
        .all()
    )
    return {"entries": [_to_dict(row) for row in rows]}


@router.post("/entries")
def create_entry(
    body: EntryIn,
    admin: dict = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    entry = FinanceEntry(**body.model_dump(), created_by_user_id=admin.get("id"),
                         updated_by_user_id=admin.get("id"))
    db.add(entry)
    db.flush()
    _audit(db, admin, "finance.entry.created", entry)
    db.commit()
    return _to_dict(entry)


@router.patch("/entries/{entry_id}")
def update_entry(
    entry_id: int,
    body: EntryPatch,
    admin: dict = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    entry = db.get(FinanceEntry, entry_id)
    if entry is None or entry.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Renglón inexistente")
    changes = body.model_dump(exclude_unset=True)
    # Re-validar con el mismo esquema que el alta: un PATCH no puede dejar
    # un período o una sección que el POST rechazaría.
    merged = {**_to_dict(entry), **changes}
    try:
        validated = EntryIn(**{k: merged.get(k) for k in EntryIn.model_fields})
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    for key in changes:
        setattr(entry, key, getattr(validated, key))
    entry.updated_by_user_id = admin.get("id")
    entry.updated_at = utcnow()
    _audit(db, admin, "finance.entry.updated", entry, fields=sorted(changes))
    db.commit()
    return _to_dict(entry)


@router.delete("/entries/{entry_id}")
def delete_entry(
    entry_id: int,
    admin: dict = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    entry = db.get(FinanceEntry, entry_id)
    if entry is None or entry.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Renglón inexistente")
    entry.deleted_at = utcnow()
    entry.updated_by_user_id = admin.get("id")
    _audit(db, admin, "finance.entry.deleted", entry)
    db.commit()
    return {"ok": True}


@router.post("/entries/import")
def import_entries(
    body: ImportIn,
    admin: dict = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    if not body.entries:
        raise HTTPException(status_code=422, detail="Sin renglones para importar")
    if len(body.entries) > MAX_IMPORT:
        raise HTTPException(status_code=422, detail=f"Máximo {MAX_IMPORT} renglones por importación")
    replaced = 0
    now = utcnow()
    if body.replace:
        keys = {(e.period, e.section) for e in body.entries}
        for period, section in keys:
            replaced += (
                db.query(FinanceEntry)
                .filter(
                    FinanceEntry.period == period,
                    FinanceEntry.section == section,
                    FinanceEntry.deleted_at.is_(None),
                )
                .update({"deleted_at": now, "updated_by_user_id": admin.get("id")},
                        synchronize_session=False)
            )
    for item in body.entries:
        db.add(FinanceEntry(**item.model_dump(), created_by_user_id=admin.get("id"),
                            updated_by_user_id=admin.get("id")))
    _audit(db, admin, "finance.entries.imported", None,
           count=len(body.entries), replaced=replaced)
    db.commit()
    return {"imported": len(body.entries), "replaced": replaced}


# ---------------------------------------------------------------------------
# Métricas de operación
# ---------------------------------------------------------------------------

def _month(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(ART).strftime("%Y-%m")


def _song_key(artist: Optional[str], title: Optional[str]) -> str:
    text = f"{artist or ''}|{title or ''}".strip().lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", text)


def _piece_type(label: Optional[str]) -> str:
    return "art_track" if (label or "").strip().lower() == ART_TRACK_LABEL else "lyric_video"


def _in_range(month: Optional[str], since: str, until: str) -> bool:
    return month is not None and since <= month <= until


@router.get("/operations")
def operations(
    since: str = Query(..., description="YYYY-MM"),
    until: str = Query(..., description="YYYY-MM"),
    admin: dict = Depends(require_super_admin),
    db: Session = Depends(get_db),
    ddb: Session = Depends(get_deliveries_db),
):
    for value in (since, until):
        if not PERIOD_RE.match(value):
            raise HTTPException(status_code=422, detail="since/until deben ser YYYY-MM")
    if since > until:
        raise HTTPException(status_code=422, detail="since posterior a until")

    # --- Entregas: primera entrega vigente de cada job, por mes y portal ---
    first: dict[str, Delivery] = {}
    for d in ddb.query(Delivery).filter(Delivery.removed_at.is_(None)).all():
        current = first.get(d.job_id)
        if current is None or d.added_at < current.added_at:
            first[d.job_id] = d
    deliveries: dict[tuple, dict] = defaultdict(lambda: {"entregas": 0, "songs": set()})
    delivered_month: dict[str, str] = {}
    for job_id, d in first.items():
        month = _month(d.added_at)
        delivered_month[job_id] = month
        if not _in_range(month, since, until):
            continue
        bucket = deliveries[(month, d.portal_id or "argentina", _piece_type(d.label))]
        bucket["entregas"] += 1
        bucket["songs"].add(_song_key(d.artist_snapshot, d.song_title_snapshot))
    delivered_rows = [
        {"month": m, "portal": p, "type": t, "entregas": v["entregas"], "canciones": len(v["songs"])}
        for (m, p, t), v in sorted(deliveries.items())
    ]

    # --- Material recibido (campañas con portal de destino) ---
    received: dict[tuple, dict] = defaultdict(lambda: {"recibidos": 0, "descartados": 0})
    item_job: dict[str, str] = {}
    for job_id, item_id in db.query(Job.job_id, Job.campaign_item_id).filter(
        Job.campaign_item_id.isnot(None)
    ):
        item_job[item_id] = job_id
    lead_days: dict[str, list[float]] = defaultdict(list)
    # Campañas reales de cliente: con portal de destino o del tenant de
    # Universal (la de los 300 de agosto no tiene portal cargado). Las de QA
    # y los canarios quedan afuera porque están canceladas.
    campaigns = {
        c.id: c for c in db.query(BatchCampaign).filter(BatchCampaign.status != "cancelled")
        if c.destination_portal or (c.tenant_id or "").startswith("universal")
    }
    if campaigns:
        items = db.query(BatchCampaignItem).filter(
            BatchCampaignItem.campaign_id.in_(list(campaigns)),
            (BatchCampaignItem.uploaded_at.isnot(None)) | (BatchCampaignItem.upload_state == "uploaded"),
        )
        for item in items:
            # Los art tracks llegan como manifiesto sin uploaded_at: la fecha
            # de alta del ítem es la de recepción.
            received_at = item.uploaded_at or item.created_at
            campaign = campaigns[item.campaign_id]
            job_id = item_job.get(item.id)
            delivery = first.get(job_id) if job_id else None
            portal = campaign.destination_portal or (delivery.portal_id if delivery else None) or "argentina"
            month = _month(received_at)
            if _in_range(month, since, until):
                bucket = received[(month, portal, campaign.kind or "lyric_video")]
                bucket["recibidos"] += 1
                if item.discard_record:
                    bucket["descartados"] += 1
            if delivery is not None and _in_range(delivered_month.get(job_id), since, until):
                uploaded = received_at if received_at.tzinfo else received_at.replace(tzinfo=timezone.utc)
                added = delivery.added_at if delivery.added_at.tzinfo else delivery.added_at.replace(tzinfo=timezone.utc)
                lead_days[delivered_month[job_id]].append(max(0.0, (added - uploaded).total_seconds() / 86400))
    received_rows = [
        {"month": m, "portal": p, "type": t, **v} for (m, p, t), v in sorted(received.items())
    ]
    lead_rows = [
        {"month": m, "mediana_dias": round(statistics.median(v), 1), "p90_dias": round(sorted(v)[int(0.9 * (len(v) - 1))], 1), "n": len(v)}
        for m, v in sorted(lead_days.items()) if v
    ]

    # --- Revisores: letras aprobadas por persona y mes ---
    names = {uid: name for uid, name in db.query(User.id, User.username)}
    reviewers: dict[tuple, set] = defaultdict(set)
    approvals = db.query(AuditLog.user_id, AuditLog.created_at, AuditLog.detail).filter(
        AuditLog.action == "batch.lyrics_and_timing_approved",
    )
    for user_id, created_at, detail in approvals:
        month = _month(created_at)
        if not _in_range(month, since, until):
            continue
        job_id = (detail or {}).get("job_id")
        if job_id:
            reviewers[(month, names.get(user_id) or f"user {user_id}")].add(job_id)
    reviewer_rows = [
        {"month": m, "reviewer": r, "canciones": len(v)} for (m, r), v in sorted(reviewers.items())
    ]

    # --- Pedidos de cambio del cliente ---
    portal_of = {d.id: d.portal_id for d in ddb.query(Delivery.id, Delivery.portal_id)}
    changes: dict[tuple, dict] = defaultdict(lambda: {"recibidos": 0, "resueltos": 0})
    open_now = 0
    for cr in ddb.query(DeliveryChangeRequest).all():
        if cr.resolved_at is None:
            open_now += 1
        portal = portal_of.get(cr.delivery_id) or "argentina"
        submitted = _month(cr.submitted_at)
        if _in_range(submitted, since, until):
            changes[(submitted, portal)]["recibidos"] += 1
        resolved = _month(cr.resolved_at)
        if _in_range(resolved, since, until):
            changes[(resolved, portal)]["resueltos"] += 1
    change_rows = [{"month": m, "portal": p, **v} for (m, p), v in sorted(changes.items())]

    return {
        "since": since,
        "until": until,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "deliveries": delivered_rows,
        "received": received_rows,
        "lead_time": lead_rows,
        "reviewers": reviewer_rows,
        "change_requests": change_rows,
        "change_requests_open": open_now,
    }


# ---------------------------------------------------------------------------
# Costos automáticos (recolector diario)
# ---------------------------------------------------------------------------

# Fuentes del recolector que se pueden usar tal cual. OpenAI queda afuera
# hasta que el recolector lea la organización de Genly: hoy su admin key es de
# otra organización y suma gasto ajeno. `fixed` tampoco: la estructura se carga
# como renglones recurrentes.
AUTO_SOURCES = {
    "railway": ("Infraestructura", "Railway"),
    "r2": ("Infraestructura", "Cloudflare R2"),
    "gcp": ("IA", "Google Cloud (Vertex)"),
    "replicate": ("IA", "Replicate"),
}


@router.get("/auto-costs")
def auto_costs(
    since: str = Query(..., description="YYYY-MM"),
    until: str = Query(..., description="YYYY-MM"),
    admin: dict = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    """Costo por mes y fuente desde ``cost_daily`` (filas ``total`` diarias).

    Devuelve también cuántos días tiene cada mes, para que la planilla marque
    un mes incompleto en vez de mostrar un total bajo como si fuera cierto.
    """
    for value in (since, until):
        if not PERIOD_RE.match(value):
            raise HTTPException(status_code=422, detail="since/until deben ser YYYY-MM")
    start = datetime.strptime(since, "%Y-%m").date()
    y, m = map(int, until.split("-"))
    end = datetime(y + (m == 12), (m % 12) + 1, 1).date()
    rows = (
        db.query(CostDaily.day, CostDaily.source, CostDaily.amount_usd,
                 CostDaily.is_estimate, CostDaily.fetched_at)
        .filter(
            CostDaily.grain == "day",
            CostDaily.dim_type == "total",
            CostDaily.day >= start,
            CostDaily.day < end,
            CostDaily.source.in_(list(AUTO_SOURCES)),
        )
        .all()
    )
    agg: dict[tuple, dict] = {}
    for day, source, amount, estimate, fetched in rows:
        key = (day.strftime("%Y-%m"), source)
        bucket = agg.setdefault(key, {"amount_usd": 0.0, "days": set(), "estimate": False, "fetched_at": None})
        bucket["amount_usd"] += float(amount or 0.0)
        bucket["days"].add(day)
        bucket["estimate"] = bucket["estimate"] or bool(estimate)
        if fetched and (bucket["fetched_at"] is None or fetched > bucket["fetched_at"]):
            bucket["fetched_at"] = fetched
    today = datetime.now(timezone.utc).date()
    out = []
    for (month, source), v in sorted(agg.items()):
        my, mm = map(int, month.split("-"))
        first = datetime(my, mm, 1).date()
        nxt = datetime(my + (mm == 12), (mm % 12) + 1, 1).date()
        # Días esperados: hasta ayer en el mes en curso (el de hoy no cerró).
        expected = (min(nxt, today) - first).days
        category, provider = AUTO_SOURCES[source]
        out.append({
            "month": month, "source": source, "category": category, "provider": provider,
            "amount_usd": round(v["amount_usd"], 2), "days": len(v["days"]),
            "expected_days": max(expected, 0), "estimate": v["estimate"],
            "fetched_at": v["fetched_at"].isoformat() if v["fetched_at"] else None,
        })
    return {"since": since, "until": until, "costs": out}
