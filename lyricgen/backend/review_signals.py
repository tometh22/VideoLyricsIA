"""Persistencia de señales de revisión automática (``review_signal_records``).

Dos señales se calculaban y se perdían, así que no se podía medir cuánto
valen:

- los puntos de la revisión rápida (``lyric_review``: "se escucha distinto",
  "falta texto"…, con el arreglo sugerido y qué oídos coincidieron), que
  vivían sólo en la caché LRU del proceso;
- las propuestas de ``repetition_reconcile`` (repeticiones del estribillo que
  faltan), de las que sólo la insertada dejaba rastro y las que un gate
  declinaba iban sólo al log.

Este módulo las guarda tal como se calcularon para cruzarlas después con los
pedidos de cambio del cliente (precisión/recall por señal). Nada del producto
lo lee.

Reglas:

- ``REVIEW_SIGNALS_PERSIST_ENABLED`` (default ``0``): apagado no escribe nada.
- Mejor esfuerzo: un error de persistencia nunca sube (se loguea y se sigue).
- La escritura va en un hilo aparte con su propia sesión corta, así el editor
  y el pipeline no esperan a la base. La cola es acotada: si se llena, se
  descarta (y se loguea) antes que acumular memoria.
- ``dedupe_key`` único: recalcular el mismo punto para el mismo job y la misma
  revisión no duplica filas (``ON CONFLICT DO NOTHING``).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger("genly.review_signals")

_TRUE = ("1", "true", "yes", "on")

KIND_LYRIC_REVIEW = "lyric_review_item"
KIND_REPETITION = "repetition_proposal"

_MAX_TEXT = 300
_MAX_OCCURRENCES = 8
_MAX_LIST = 12
_MAX_PAYLOAD_BYTES = 6000
_MAX_PENDING_BATCHES = 64

_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()
_pending = threading.BoundedSemaphore(_MAX_PENDING_BATCHES)


def is_enabled() -> bool:
    return os.environ.get(
        "REVIEW_SIGNALS_PERSIST_ENABLED", "0"
    ).strip().lower() in _TRUE


def pipeline_release() -> str:
    return str(
        os.environ.get("RELEASE")
        or os.environ.get("RAILWAY_GIT_COMMIT_SHA")
        or "unknown"
    )[:64]


def _f(value) -> float | None:
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return None


def _text(value, limit: int = _MAX_TEXT) -> str:
    return str(value if value is not None else "")[:limit]


def segments_hash(segments) -> str:
    """Huella de la letra y el timing que se miraron (texto, inicio, fin)."""
    rows = [
        (str(s.get("text") or ""), _f(s.get("start")), _f(s.get("end")))
        for s in (segments or []) if isinstance(s, dict)
    ]
    encoded = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _dedupe_key(*parts) -> str:
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _bounded_payload(payload: dict, essentials: tuple[str, ...]) -> dict:
    """Corta el payload si se pasa del tope: quedan sólo los campos clave."""
    try:
        size = len(json.dumps(payload, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        size = _MAX_PAYLOAD_BYTES + 1
    if size <= _MAX_PAYLOAD_BYTES:
        return payload
    slim = {k: payload[k] for k in essentials if k in payload}
    if isinstance(payload.get("occurrences"), list):
        slim["occurrences"] = payload["occurrences"][:2]
    slim["truncated"] = True
    return slim


def _clean_fix(fix) -> dict:
    if not isinstance(fix, dict):
        return {}
    out = {}
    for key, value in list(fix.items())[:_MAX_LIST]:
        if isinstance(value, str):
            out[key] = _text(value)
        elif isinstance(value, (int, float, bool)) or value is None:
            out[key] = value
        elif key == "lines" and isinstance(value, list):
            out[key] = [
                {"text": _text(x.get("text"))} for x in value[:_MAX_LIST]
                if isinstance(x, dict)
            ]
    return out


# ---------------------------------------------------------------------------
# Construcción de filas (puro, sin I/O)
# ---------------------------------------------------------------------------

def lyric_review_records(*, job_id: str, tenant_id: str | None, revision,
                         current_segments, review: dict) -> list[dict]:
    """Una fila por punto de la revisión rápida (agrupado como lo ve el
    editor; las repeticiones del mismo arreglo van en ``occurrences``)."""
    items = (review or {}).get("items") or []
    if not job_id or not items:
        return []
    seg_hash = segments_hash(current_segments)
    release = pipeline_release()
    sources_meta = (review or {}).get("sources") or {}
    similarity = sources_meta.get("similarity") if isinstance(sources_meta, dict) else None
    records = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "")
        if not item_id:
            item_id = _dedupe_key(*(item.get("keys") or []))[:12]
        occurrences = [o for o in (item.get("occurrences") or []) if isinstance(o, dict)]
        first = occurrences[0] if occurrences else {}
        payload = {
            "item_id": item_id,
            "required": bool(item.get("required")),
            "listen": bool(item.get("listen")),
            "group": _text(item.get("group"), 40),
            "sources": [_text(s, 40) for s in (item.get("sources") or [])][:_MAX_LIST],
            "keys": [_text(k) for k in (item.get("keys") or [])][:_MAX_LIST],
            "alternatives": [_text(a) for a in (item.get("alternatives") or [])
                             if isinstance(a, str)][:_MAX_LIST],
            "occurrence_count": len(occurrences),
            "occurrences": [
                {
                    "line_index": o.get("line_index"),
                    "line_segment_id": _text(o.get("line_segment_id"), 64)
                    if o.get("line_segment_id") is not None else None,
                    "start": _f(o.get("start")),
                    "end": _f(o.get("end")),
                    "before": _text(o.get("before")),
                    "after": _text(o.get("after")),
                    "fix": _clean_fix(o.get("fix")),
                }
                for o in occurrences[:_MAX_OCCURRENCES]
            ],
            "review_schema": _text((review or {}).get("schema"), 40),
            "official_origin": _text(sources_meta.get("official_origin"), 20)
            if isinstance(sources_meta, dict) else "",
            "similarity": similarity if isinstance(similarity, dict) else {},
        }
        line_index = first.get("line_index")
        records.append({
            "job_id": str(job_id)[:12],
            "tenant_id": str(tenant_id)[:100] if tenant_id else None,
            "kind": KIND_LYRIC_REVIEW,
            "item_type": _text(item.get("kind") or "unknown", 40),
            "decision": "proposed",
            "reason": None,
            "editor_revision": int(revision) if isinstance(revision, int) else None,
            "segments_hash": seg_hash,
            "pipeline_release": release,
            "line_index": line_index if isinstance(line_index, int) else None,
            "start_s": _f(first.get("start")),
            "end_s": _f(first.get("end")),
            "payload": _bounded_payload(payload, (
                "item_id", "required", "listen", "sources", "keys", "occurrence_count",
            )),
            # Una vez por job + revisión + punto.
            "dedupe_key": _dedupe_key(job_id, KIND_LYRIC_REVIEW, revision, item_id),
        })
    return records


def repetition_records(*, job_id: str, segments, proposals, gate_reason: str | None = None) -> list[dict]:
    """Una fila por propuesta de ``repetition_reconcile``. ``gate_reason``
    marca las que un gate externo no dejó aplicar (corrida en sombra): lo
    que se habría aplicado queda ``declined`` con ese motivo."""
    if not job_id or not proposals:
        return []
    seg_hash = segments_hash(segments)
    release = pipeline_release()
    records = []
    for proposal in proposals:
        if not isinstance(proposal, dict):
            continue
        decision = str(proposal.get("decision") or "declined")
        reason = proposal.get("reason")
        if gate_reason and decision == "applied":
            decision, reason = "declined", gate_reason
        item_type = _text(proposal.get("item_type") or "repetition_insert", 40)
        payload = {
            k: (_text(v) if isinstance(v, str) else v)
            for k, v in proposal.items()
            if k not in {"decision", "reason", "item_type", "line_index", "start", "end"}
        }
        if gate_reason:
            payload["shadow"] = True
            payload["would_decision"] = str(proposal.get("decision") or "")
        start, end = _f(proposal.get("start")), _f(proposal.get("end"))
        line_index = proposal.get("line_index")
        records.append({
            "job_id": str(job_id)[:12],
            "tenant_id": None,
            "kind": KIND_REPETITION,
            "item_type": item_type,
            "decision": decision[:16],
            "reason": _text(reason, 80) if reason else None,
            "editor_revision": None,
            "segments_hash": seg_hash,
            "pipeline_release": release,
            "line_index": line_index if isinstance(line_index, int) else None,
            "start_s": start,
            "end_s": end,
            "payload": _bounded_payload(payload, (
                "group_id", "group_text", "group_size", "ratio",
            )),
            "dedupe_key": _dedupe_key(
                job_id, KIND_REPETITION, seg_hash, item_type,
                proposal.get("group_text"), start, end, decision, reason,
            ),
        })
    return records


# ---------------------------------------------------------------------------
# Escritura
# ---------------------------------------------------------------------------

def write_records(records: list[dict], *, session_factory=None) -> int:
    """Inserta ``records`` ignorando los ya guardados (``dedupe_key``).
    Devuelve cuántas filas nuevas quedaron. Nunca levanta."""
    if not records:
        return 0
    db = None
    try:
        from database import ReviewSignalRecord, utcnow
        if session_factory is None:
            from database import SessionLocal as session_factory
        db = session_factory()
        dialect = db.get_bind().dialect.name
        if dialect == "postgresql":
            from sqlalchemy import text
            # Una escritura de diagnóstico nunca espera a nadie.
            db.execute(text("SET LOCAL lock_timeout = '2s'"))
            db.execute(text("SET LOCAL statement_timeout = '5s'"))
        missing_tenant = {r["job_id"] for r in records if not r.get("tenant_id")}
        tenants: dict[str, str] = {}
        if missing_tenant:
            from database import Job
            tenants = {
                jid: tid for jid, tid in db.query(Job.job_id, Job.tenant_id)
                .filter(Job.job_id.in_(sorted(missing_tenant))).all()
            }
        now = utcnow()
        rows = []
        seen: set[str] = set()
        for record in records:
            if record["dedupe_key"] in seen:
                continue
            seen.add(record["dedupe_key"])
            rows.append({
                **record,
                "tenant_id": record.get("tenant_id") or tenants.get(record["job_id"]),
                "created_at": now,
            })
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        elif dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert
        else:  # pragma: no cover - sólo PostgreSQL y SQLite
            return 0
        stmt = insert(ReviewSignalRecord.__table__).values(rows)
        stmt = stmt.on_conflict_do_nothing(index_elements=["dedupe_key"])
        result = db.execute(stmt)
        db.commit()
        inserted = result.rowcount if result.rowcount is not None and result.rowcount >= 0 else 0
        return int(inserted)
    except Exception as exc:
        logger.warning("[REVIEW-SIGNALS] persist failed (%d records): %r",
                       len(records), exc)
        if db is not None:
            try:
                db.rollback()
            except Exception:  # pragma: no cover
                pass
        return 0
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:  # pragma: no cover
                pass


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="review-signals")
        return _executor


def _run(records: list[dict]) -> None:
    try:
        write_records(records)
    finally:
        _pending.release()


def submit(records: list[dict]) -> bool:
    """Encola la escritura en el hilo de fondo. ``False`` si se descartó."""
    if not records:
        return False
    if not _pending.acquire(blocking=False):
        logger.warning("[REVIEW-SIGNALS] queue full, dropped %d records", len(records))
        return False
    try:
        _get_executor().submit(_run, records)
        return True
    except Exception as exc:
        _pending.release()
        logger.warning("[REVIEW-SIGNALS] submit failed: %r", exc)
        return False


# ---------------------------------------------------------------------------
# Puntos de escritura (lo que llaman el editor y el pipeline)
# ---------------------------------------------------------------------------

def record_lyric_review(document, job, review: dict) -> None:
    """Guarda los puntos de una revisión rápida recién calculada. Nunca
    levanta y no toca la sesión del request."""
    if not is_enabled():
        return
    try:
        records = lyric_review_records(
            job_id=str(getattr(document, "job_id", "") or ""),
            tenant_id=getattr(document, "tenant_id", None) or getattr(job, "tenant_id", None),
            revision=getattr(document, "revision", None),
            current_segments=list(getattr(document, "current_segments", None) or []),
            review=review,
        )
        submit(records)
    except Exception as exc:
        logger.warning("[REVIEW-SIGNALS] lyric_review capture failed job=%s: %r",
                       getattr(document, "job_id", None), exc)


def record_repetition(job_id: str, segments, stats: dict, *, gate_reason: str | None = None) -> None:
    """Guarda las propuestas (aplicadas y declinadas) de una corrida de
    ``repetition_reconcile``. Nunca levanta."""
    if not is_enabled():
        return
    try:
        records = repetition_records(
            job_id=str(job_id or ""), segments=segments,
            proposals=(stats or {}).get("proposals") or [],
            gate_reason=gate_reason,
        )
        submit(records)
    except Exception as exc:
        logger.warning("[REVIEW-SIGNALS] repetition capture failed job=%s: %r", job_id, exc)
