"""Resumen antes de publicar al portal de UMG: qué nadie escuchó y qué nadie miró.

POR QUÉ EXISTE
--------------
Sobre 1.034 puntos que UMG reclamó por el portal (sep-nov 2026), el 29 % eran
errores de la máquina en líneas que el revisor nunca tocó y otro 29 % errores
de la máquina en líneas que tocó y dejó mal; casi la mitad de esos errores ya
estaban resueltos en una fuente que teníamos (sobre todo la letra oficial).
Desde 1.1.178 la Revisión rápida es orientativa (ningún aviso frena la
aprobación) y antes ya se medía que casi nadie abre sus sugerencias. En vez de
frenar al revisor en cada canción, hay UN control al publicar:

1. **Cobertura de escucha.** Las líneas de la letra que se va a publicar que
   nunca sonaron en el editor (evento ``editor_audio_played``, 1.1.122). Una
   línea cuenta como escuchada si sonó al menos la mitad de su tramo, por
   cualquier persona y en cualquier sesión anterior a la publicación. Sólo
   se listan líneas cuando la medición es completa (``measured``): medido el
   19-nov, la mayoría de las canciones aprobadas tenían heartbeats con el
   audio sonando y ningún tramo registrado, así que "sin datos" nunca se
   presenta como "nadie escuchó".
2. **Avisos de alta precisión sin mirar.** Puntos de la Revisión rápida que
   siguen pendientes (ni aplicados ni marcados "está bien así") y vienen de
   fuentes precisas: la letra oficial, dos oídos de acuerdo (testigo + Gemini)
   y el estilo del portal (punto final, ¿? sin par, tildes que faltan, voseo
   incluido). El timing no entra: la Revisión rápida no tiene un aviso de
   timing confirmado (``_rule_timing`` está apagado por baja precisión).

``PUBLISH_REVIEW_SUMMARY``:

- ``observe`` (por defecto): se calcula y se registra en el audit log local
  (``delivery.publish_review``) sin mostrar nada ni pedir confirmación.
- ``prompt``: si hay algo que mostrar, la publicación devuelve 409
  ``publish_review_required`` con el resumen; "Publicar igual" reenvía el
  ``token`` del resumen visto y queda registrado quién, cuántas líneas sin
  escuchar y cuántos avisos.
- ``off``: no se calcula.

Nunca bloquea de forma dura: un token vigente siempre publica, y si el
resumen no se puede calcular la publicación sigue (fail-open, registrado).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from collections import defaultdict
from typing import Any, Iterable

from listening_coverage import (
    HEARTBEAT_EVENT,
    PLAYBACK_EVENT,
    merge_intervals,
    line_heard,
    segment_span,
)

logger = logging.getLogger(__name__)

SCHEMA = "publish-review-v1"
REQUIRED_CODE = "publish_review_required"
AUDIT_ACTION = "delivery.publish_review"
MODES = ("off", "observe", "prompt")
# Lo que viaja al navegador por canción; el audit guarda siempre los totales.
MAX_LISTED_LINES = 200
MAX_LISTED_WARNINGS = 60
MAX_OCCURRENCES = 12
MAX_LINE_TEXT = 140

TEXT_KINDS = {"heard_different", "missing"}
# Estilo por portal con arreglo inequívoco. Los paréntesis son sugerencia y
# las exclamaciones dependen del portal: no entran.
STYLE_KINDS = {"terminal_period", "question_marks", "accent_missing"}
EARS = {"witness", "gemini", "official"}
CATEGORIES = ("official", "two_ears", "style")
# Sólo con estas mediciones se listan líneas "sin escuchar".
RELIABLE_LISTENING = {"measured"}
CATEGORY_LABELS = {
    "official": "Letra oficial",
    "two_ears": "Dos oídos de acuerdo",
    "style": "Estilo del portal",
}


def mode() -> str:
    value = os.environ.get("PUBLISH_REVIEW_SUMMARY", "observe").strip().lower()
    return value if value in MODES else "observe"


# --------------------------------------------------------------------------
# Puro
# --------------------------------------------------------------------------

def warning_category(item: dict) -> str | None:
    """Categoría de alta precisión de un punto de la Revisión rápida, o None."""
    if not isinstance(item, dict) or item.get("dismissed"):
        return None
    kind = item.get("kind")
    sources = set(item.get("sources") or ())
    if kind in TEXT_KINDS:
        if item.get("listen"):
            return None  # un solo oído automático: se ofrece escuchar, no corregir
        if "official" in sources:
            return "official"
        if len(sources & EARS) >= 2:
            return "two_ears"
        return None
    if kind in STYLE_KINDS and item.get("style_rule") and item.get("required"):
        return "style"
    return None


def high_precision_warnings(review: dict | None) -> list[dict]:
    out = []
    for item in (review or {}).get("items") or []:
        category = warning_category(item)
        if category is None:
            continue
        occurrences = [o for o in item.get("occurrences") or [] if isinstance(o, dict)]
        out.append({
            "id": str(item.get("id") or ""),
            "kind": item.get("kind"),
            "category": category,
            "title": item.get("title") or "",
            "why": item.get("why") or "",
            "count": len(occurrences) or 1,
            "occurrences": [{
                "line_index": o.get("line_index"),
                "start": o.get("start"),
                "end": o.get("end"),
                "before": str(o.get("before") or "")[:MAX_LINE_TEXT],
                "after": str(o.get("after") or "")[:MAX_LINE_TEXT],
            } for o in occurrences[:MAX_OCCURRENCES]],
        })
    return out


def unheard_lines(segments: Iterable[dict], ranges) -> tuple[int, list[dict]]:
    """(líneas con letra, líneas sin escuchar). Sólo cuentan líneas con texto
    y tiempo válido; una línea vacía o instrumental no se escucha."""
    merged = merge_intervals(ranges)
    total = 0
    unheard = []
    for index, segment in enumerate(segments or []):
        if not isinstance(segment, dict) or not str(segment.get("text") or "").strip():
            continue
        span = segment_span(segment)
        if span is None:
            continue
        total += 1
        if not line_heard(span, merged):
            unheard.append({
                "line_index": index,
                "segment_id": str(segment.get("segment_id") or "") or None,
                "start": round(span[0] / 1000, 2),
                "end": round(span[1] / 1000, 2),
                "text": str(segment.get("text") or "").strip()[:MAX_LINE_TEXT],
            })
    return total, unheard


def summary_token(job_id: str, revision: int | None, unheard: list[dict], warnings: list[dict]) -> str:
    """Identidad del resumen que vio la persona: misma letra, mismas líneas sin
    escuchar, mismos avisos. Si algo cambia, hay que volver a mirarlo."""
    payload = [SCHEMA, job_id, int(revision or 0),
               [line["line_index"] for line in unheard],
               sorted(warning["id"] for warning in warnings)]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


def summarize(job_id: str, segments: list[dict], *, revision: int | None, ranges,
              listening_status: str, review: dict | None, title: str = "",
              artist: str = "") -> dict:
    lines_total, unheard = unheard_lines(segments, ranges)
    # Sin telemetría confiable no se afirma que nadie escuchó: se calcula para
    # el audit (``lines_unheard_raw``) pero no se lista ni pide confirmación.
    listening_known = listening_status in RELIABLE_LISTENING
    raw_unheard = len(unheard)
    if not listening_known:
        unheard = []
    warnings = high_precision_warnings(review)
    by_category = {category: 0 for category in CATEGORIES}
    for warning in warnings:
        by_category[warning["category"]] += 1
    return {
        "schema": SCHEMA,
        "job_id": job_id,
        "title": title,
        "artist": artist,
        "editor_revision": int(revision or 0),
        "listening_status": listening_status,
        "listening_known": listening_known,
        "lines_total": lines_total,
        "lines_unheard": len(unheard),
        "lines_unheard_raw": raw_unheard,
        "unheard": unheard[:MAX_LISTED_LINES],
        "warnings_count": len(warnings),
        "warnings_by_category": by_category,
        "warnings": warnings[:MAX_LISTED_WARNINGS],
        "review_unavailable": bool((review or {}).get("error")),
        "needs_attention": bool(unheard or warnings),
        "token": summary_token(job_id, revision, unheard, warnings),
    }


def totals(summaries: Iterable[dict]) -> dict:
    items = list(summaries)
    return {
        "songs": len(items),
        "songs_needing_attention": sum(1 for s in items if s.get("needs_attention")),
        "lines_unheard": sum(int(s.get("lines_unheard") or 0) for s in items),
        "lines_total": sum(int(s.get("lines_total") or 0) for s in items),
        "warnings": sum(int(s.get("warnings_count") or 0) for s in items),
    }


def pending(summaries: dict[str, dict], tokens: dict[str, str] | None) -> list[dict]:
    """Resúmenes con algo para mostrar que la persona todavía no confirmó."""
    tokens = tokens or {}
    return [s for s in summaries.values()
            if s.get("needs_attention") and tokens.get(s["job_id"]) != s["token"]]


def required_detail(pending_summaries: list[dict]) -> dict:
    total = totals(pending_summaries)
    return {
        "code": REQUIRED_CODE,
        "message": ("Antes de publicar, mirá qué líneas nadie escuchó y qué avisos de la revisión "
                    "quedaron sin mirar. Podés corregir o publicar igual."),
        "summaries": pending_summaries,
        "totals": total,
    }


# --------------------------------------------------------------------------
# Con base de datos (sólo lecturas; el audit lo agrega quien publica)
# --------------------------------------------------------------------------

def applies_to(job) -> bool:
    """Sólo videos con letra: un Art Track no tiene líneas que escuchar."""
    return job is not None and not bool((getattr(job, "render_params", None) or {}).get("art_track"))


def _playback_by_job(db, job_ids: list[str]) -> tuple[dict[str, list], dict[str, str]]:
    """Tramos reproducidos por canción y si esa medición es confiable.

    ``measured``: hay tramos y toda persona que tuvo el audio sonando
    (heartbeat del servidor con ``task='listen'``) también los registró.
    ``incomplete``: alguien escuchó según el heartbeat pero sus tramos no
    llegaron (``/analytics/events`` perdido: medido el 19-nov, el tenant de
    UMG no registró ninguno entre el 19-oct y el 17-nov). ``no_playback``:
    el editor estuvo abierto sin audio. ``no_editor_activity``: nada.
    """
    from database import ProductEvent

    ranges: dict[str, list] = defaultdict(list)
    players: dict[str, set] = defaultdict(set)
    listeners: dict[str, set] = defaultdict(set)
    opened: set[str] = set()
    if not job_ids:
        return ranges, {}
    for job_id, user_id, properties in (
        db.query(ProductEvent.job_id, ProductEvent.user_id, ProductEvent.properties)
        .filter(ProductEvent.job_id.in_(job_ids))
        .filter(ProductEvent.name == PLAYBACK_EVENT)
        .all()
    ):
        ranges[job_id].extend((properties or {}).get("ranges") or [])
        players[job_id].add(user_id)
    for job_id, user_id, task in (
        db.query(ProductEvent.job_id, ProductEvent.user_id, ProductEvent.properties["task"].as_string())
        .filter(ProductEvent.job_id.in_(job_ids))
        .filter(ProductEvent.name == HEARTBEAT_EVENT)
        .distinct()
        .all()
    ):
        opened.add(job_id)
        if task == "listen":
            listeners[job_id].add(user_id)
    status: dict[str, str] = {}
    for job_id in job_ids:
        if listeners[job_id] - players[job_id]:
            status[job_id] = "incomplete"
        elif players[job_id]:
            status[job_id] = "measured"
        elif job_id in opened:
            status[job_id] = "no_playback"
        else:
            status[job_id] = "no_editor_activity"
    return ranges, status


def build_for_jobs(db, jobs: list) -> dict[str, dict]:
    """Resumen por canción (sólo videos con letra). Lecturas, sin bloqueos nuevos."""
    from database import EditorDocument
    from lyric_review_sources import review_for_document

    jobs = [job for job in jobs if applies_to(job)]
    if not jobs:
        return {}
    job_ids = [job.job_id for job in jobs]
    documents = {
        document.job_id: document
        for document in db.query(EditorDocument).filter(EditorDocument.job_id.in_(job_ids)).all()
    }
    ranges, status = _playback_by_job(db, job_ids)
    out: dict[str, dict] = {}
    for job in jobs:
        document = documents.get(job.job_id)
        if document is not None:
            segments = list(document.current_segments or [])
            revision = document.revision
            # Sin búsqueda de red: publicar no dispara lrclib.
            review = review_for_document(db, document, job, fetch_official=False)
        else:
            raw = getattr(job, "segments_json", None)
            if isinstance(raw, dict):
                raw = raw.get("segments")
            segments = [s for s in raw if isinstance(s, dict)] if isinstance(raw, list) else []
            revision = getattr(job, "segments_revision", None)
            review = None
        out[job.job_id] = summarize(
            job.job_id, segments, revision=revision, ranges=ranges.get(job.job_id, []),
            listening_status=status.get(job.job_id, "no_editor_activity"), review=review,
            title=str(job.song_title or ""), artist=str(job.artist or ""),
        )
    return out


def evaluate(db, jobs: list, tokens: dict[str, str] | None = None) -> dict[str, Any]:
    """Decide qué hacer con una publicación de ``jobs``.

    Devuelve ``mode``, ``summaries`` (por job) y ``pending`` (los que en modo
    ``prompt`` todavía necesitan que alguien vea el resumen). Nunca levanta:
    si el cálculo falla, publica y lo deja registrado como ``unavailable``.
    """
    current = mode()
    if current == "off":
        return {"mode": current, "summaries": {}, "pending": [], "error": None}
    try:
        summaries = build_for_jobs(db, jobs)
    except Exception:
        logger.warning("[PUBLISH-REVIEW] summary unavailable", exc_info=True)
        return {"mode": current, "summaries": {}, "pending": [], "error": "unavailable"}
    return {
        "mode": current,
        "summaries": summaries,
        "pending": pending(summaries, tokens) if current == "prompt" else [],
        "error": None,
    }


def audit_entries(user_id, decision: dict, tokens: dict[str, str] | None, source: str,
                  job_ids: Iterable[str] | None = None, **extra) -> list:
    """Una fila de audit log LOCAL por canción publicada: quién, cuántas líneas
    sin escuchar, cuántos avisos, y si vio el resumen y publicó igual."""
    from database import AuditLog

    if decision.get("mode") == "off":
        return []
    tokens = tokens or {}
    wanted = set(job_ids) if job_ids is not None else None
    rows = []
    if decision.get("error"):
        rows.append(AuditLog(user_id=user_id, action=AUDIT_ACTION, detail={
            "mode": decision["mode"], "source": source, "error": decision["error"],
            "job_ids": sorted(wanted or [])[:200], **extra,
        }))
        return rows
    for job_id, summary in (decision.get("summaries") or {}).items():
        if wanted is not None and job_id not in wanted:
            continue
        shown = decision["mode"] == "prompt" and summary["needs_attention"]
        rows.append(AuditLog(user_id=user_id, action=AUDIT_ACTION, detail={
            "job_id": job_id,
            "mode": decision["mode"],
            "source": source,
            "shown": shown,
            "published_anyway": shown and tokens.get(job_id) == summary["token"],
            "editor_revision": summary["editor_revision"],
            "listening_status": summary["listening_status"],
            "lines_total": summary["lines_total"],
            "lines_unheard": summary["lines_unheard"],
            "lines_unheard_raw": summary["lines_unheard_raw"],
            "unheard_line_indexes": [line["line_index"] for line in summary["unheard"]],
            "warnings": summary["warnings_count"],
            "warnings_by_category": summary["warnings_by_category"],
            "warning_ids": [warning["id"] for warning in summary["warnings"]],
            "review_unavailable": summary["review_unavailable"],
            "token": summary["token"],
            **extra,
        }))
    return rows
