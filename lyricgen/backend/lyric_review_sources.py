"""Fuentes con base de datos para la revisión rápida (``lyric_review``).

- Letra oficial: la que pegó el operador para esta canción, la de la
  planilla de la campaña o, fuera de las campañas batch, la de lrclib. Las
  campañas batch son "sólo audio" (``reference_hypothesis.audio_only_batch_mode``):
  para ellas nunca se busca ni se usa letra externa.
- Memoria de correcciones: lo que los revisores del MISMO tenant ya
  corrigieron en otras canciones del mismo artista (máquina → aprobado).

Nada de esto corre con filas bloqueadas ni retiene conexiones durante una
llamada de red; el resultado se cachea por contenido para que el autoguardado
no recalcule lo mismo.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
import threading
import time
from collections import OrderedDict

from lyric_review import (
    LyricReviewPending,
    build_review,
    fold,
    mode,
    required_for_scope,
    sound,
)

logger = logging.getLogger(__name__)

_OPERATOR_PREFIX = "ref:"
_FETCHED_PREFIX = "lrv:"
_MEMORY_TTL_S = 600.0
_FETCH_RETRY_S = 6 * 3600.0
_CACHE_MAX = 256


class _Lru(OrderedDict):
    def __init__(self, maxsize: int = _CACHE_MAX):
        super().__init__()
        self.maxsize = maxsize
        self.lock = threading.Lock()

    def get_fresh(self, key, ttl: float | None = None):
        with self.lock:
            hit = super().get(key)
            if hit is None:
                return None
            if ttl is not None and time.monotonic() - hit[0] > ttl:
                return None
            self.move_to_end(key)
            return hit[1]

    def put(self, key, value) -> None:
        with self.lock:
            self[key] = (time.monotonic(), value)
            self.move_to_end(key)
            while len(self) > self.maxsize:
                self.popitem(last=False)


_memory_cache = _Lru()
_review_cache = _Lru(128)
_fetch_attempted = _Lru(1024)
# Como mucho dos búsquedas en lrclib a la vez por proceso: nunca compiten con
# el tráfico de la API por las conexiones ni por los hilos.
_fetch_slots = threading.BoundedSemaphore(2)


def _operator_key(job_id: str) -> str:
    return f"{_OPERATOR_PREFIX}{job_id}"


def _fetched_key(artist: str, title: str) -> str:
    digest = hashlib.sha1(f"{fold(artist).strip()}|{fold(title).strip()}".encode()).hexdigest()
    return f"{_FETCHED_PREFIX}{digest[:16]}"


def _external_lyrics_allowed(job) -> bool:
    """Las campañas batch se referencian sólo con el audio."""
    return str(getattr(job, "workload_class", "") or "").lower() != "batch"


def official_text(db, job) -> tuple[str, str]:
    """(texto, origen) de la mejor letra oficial disponible, sin red."""
    from database import BatchCampaignItem, LyricsCache

    row = db.query(LyricsCache).filter(LyricsCache.cache_key == _operator_key(job.job_id)).first()
    if row and (row.lyrics or "").strip():
        return row.lyrics, "operator"
    item_id = getattr(job, "campaign_item_id", None)
    if item_id:
        item = db.query(BatchCampaignItem).filter(BatchCampaignItem.id == item_id).first()
        ref = dict((item.render_overrides or {}) if item else {}).get("source_reference")
        if isinstance(ref, dict) and str(ref.get("text") or "").strip():
            return str(ref["text"]), "sheet"
    if not _external_lyrics_allowed(job):
        return "", ""
    artist, title = str(job.artist or ""), str(job.song_title or "")
    if not artist or not title:
        return "", ""
    row = db.query(LyricsCache).filter(LyricsCache.cache_key == _fetched_key(artist, title)).first()
    if row and (row.lyrics or "").strip():
        return row.lyrics, "lrclib"
    from pipeline import _lrclib_cache_key, _lrclib_title_matches

    row = db.query(LyricsCache).filter(
        LyricsCache.cache_key == _lrclib_cache_key(artist, title)).first()
    if row and row.lyrics:
        try:
            cached = json.loads(row.lyrics)
        except (TypeError, ValueError):
            cached = {}
        if _lrclib_title_matches(title, cached.get("source_track_name") or ""):
            text = cached.get("plain")
            if not text and cached.get("synced"):
                from forced_align import lrc_to_plain_text
                text = lrc_to_plain_text(cached["synced"])
            if (text or "").strip():
                return text, "lrclib"
    return "", ""


def save_operator_reference(db, job, text: str) -> None:
    """Letra oficial pegada por el operador: sólo referencia, no re-sincroniza."""
    from database import LyricsCache
    from datetime import datetime, timezone

    key = _operator_key(job.job_id)
    row = db.query(LyricsCache).filter(LyricsCache.cache_key == key).first()
    text = str(text or "").strip()[:20000]
    if row is None:
        db.add(LyricsCache(
            cache_key=key, artist=str(job.artist or "")[:255],
            title=str(job.song_title or job.job_id)[:255], lyrics=text,
            source_urls=[], fetched_at=datetime.now(timezone.utc),
            fetched_by_model="operator",
        ))
    else:
        row.lyrics = text
        row.fetched_at = datetime.now(timezone.utc)


def schedule_official_fetch(job) -> None:
    """Busca la letra en lrclib en un hilo aparte y la deja en una caché
    propia del editor (nunca en la del pipeline). La llamada de red se hace
    SIN sesión de base abierta; la escritura usa una sesión corta después."""
    if os.environ.get("LYRIC_REVIEW_FETCH_OFFICIAL", "1").strip() == "0":
        return
    if not _external_lyrics_allowed(job):
        return
    artist, title = str(job.artist or "").strip(), str(job.song_title or "").strip()
    if not artist or not title:
        return
    key = _fetched_key(artist, title)
    if _fetch_attempted.get_fresh(key, _FETCH_RETRY_S) is not None:
        return
    if not _fetch_slots.acquire(blocking=False):
        return  # hay búsquedas en curso: se intenta en el próximo guardado
    _fetch_attempted.put(key, True)

    def run():
        try:
            from pipeline import _fetch_lrclib
            found = _fetch_lrclib(artist, title, db=None) or {}
            text = found.get("plain")
            if not text and found.get("synced"):
                from forced_align import lrc_to_plain_text
                text = lrc_to_plain_text(found["synced"])
            if not (text or "").strip():
                return
            from database import LyricsCache, SessionLocal
            from datetime import datetime, timezone
            db = SessionLocal()
            try:
                if db.query(LyricsCache).filter(LyricsCache.cache_key == key).first() is None:
                    db.add(LyricsCache(
                        cache_key=key, artist=artist[:255], title=title[:255],
                        lyrics=text[:20000], source_urls=[],
                        fetched_at=datetime.now(timezone.utc), fetched_by_model="lrclib-editor",
                    ))
                    db.commit()
            except Exception:  # pragma: no cover - mejor esfuerzo
                db.rollback()
                raise
            finally:
                db.close()
        except Exception:  # pragma: no cover - mejor esfuerzo
            logger.exception("[LYRIC-REVIEW] official lyrics fetch failed")
        finally:
            _fetch_slots.release()

    threading.Thread(target=run, name="lyric-review-official", daemon=True).start()


def mine_correction_pairs(documents: list[tuple[list, list]]) -> dict[str, str]:
    """Pares palabra-de-máquina → palabra-aprobada, uno a uno, de las
    canciones ya aprobadas de un artista. Se descarta un par si la palabra
    "equivocada" sigue apareciendo en alguna letra aprobada: entonces es una
    palabra legítima y la corrección fue de contexto, no de oído."""
    from heard_words import aligned_opcodes

    pairs: dict[str, str] = {}
    approved_vocab: set[str] = set()
    raw_pairs: list[tuple[str, str]] = []
    for original, approved in documents:
        orig_raw = [w for s in (original or []) for w in str(s.get("text") or "").split()]
        appr_raw = [w for s in (approved or []) for w in str(s.get("text") or "").split()]
        a = [fold(w).strip(".,;:!?¡¿\"'()") for w in orig_raw]
        b = [fold(w).strip(".,;:!?¡¿\"'()") for w in appr_raw]
        approved_vocab.update(b)
        for tag, i1, i2, j1, j2 in aligned_opcodes(a, b):
            if tag != "replace" or (i2 - i1) != (j2 - j1):
                continue
            for k in range(i2 - i1):
                wrong, right_raw = a[i1 + k], appr_raw[j1 + k]
                right = b[j1 + k]
                if len(wrong) < 4 or len(right) < 4 or wrong == right:
                    continue
                # Conjugaciones y concordancias (toda/todo, fuera/fueron) son
                # correcciones de contexto, no de oído.
                if wrong[:-2] == right[:-2] or wrong.startswith(right) or right.startswith(wrong):
                    continue
                # Se parecen al OÍDO (chuchu / Xuxú, capulus / Kapelusz), no
                # sólo por letras.
                if difflib.SequenceMatcher(a=sound(wrong), b=sound(right)).ratio() < 0.6:
                    continue
                raw_pairs.append((wrong, right_raw.strip(".,;:!?¡¿\"'()")))
    for wrong, right in raw_pairs:
        if wrong in approved_vocab:
            continue
        pairs[wrong] = right
    return pairs


def memory_pairs(db, job) -> dict[str, str]:
    """Correcciones del mismo artista, sólo dentro del mismo tenant y sin
    copias de piloto."""
    artist = str(job.artist or "").strip()
    tenant = str(getattr(job, "tenant_id", "") or "")
    if not artist or not tenant:
        return {}
    cache_key = (tenant, fold(artist))
    hit = _memory_cache.get_fresh(cache_key, _MEMORY_TTL_S)
    if hit is not None:
        return hit
    from database import EditorDocument, EditorVersion, Job

    escaped = artist.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    query = db.query(Job.job_id).filter(
        Job.tenant_id == tenant,
        Job.job_id != job.job_id,
        Job.artist.ilike(escaped, escape="\\"),
    )
    if hasattr(Job, "pilot_id"):
        query = query.filter(Job.pilot_id.is_(None))
    job_ids = [jid for (jid,) in query.order_by(Job.created_at.desc()).limit(40).all()]
    documents = []
    for jid in job_ids:
        doc = db.query(EditorDocument.original_segments).filter(
            EditorDocument.job_id == jid, EditorDocument.tenant_id == tenant).first()
        ver = db.query(EditorVersion.segments).filter(
            EditorVersion.job_id == jid, EditorVersion.tenant_id == tenant,
            EditorVersion.is_approved.is_(True),
        ).order_by(EditorVersion.revision.desc()).first()
        if doc and ver:
            documents.append((doc[0] or [], ver[0] or []))
    pairs = mine_correction_pairs(documents)
    _memory_cache.put(cache_key, pairs)
    return pairs


def effective_mode(job) -> str:
    """``enforce`` sólo donde corresponde. Con ``LYRIC_REVIEW_ENFORCE_TENANTS``
    (lista separada por comas) se bloquea en esos tenants; sin esa variable,
    en las campañas batch (UMG). En el resto el panel se muestra sin
    bloquear."""
    base = mode()
    if base != "enforce" or job is None:
        return base
    tenants = {t.strip() for t in os.environ.get("LYRIC_REVIEW_ENFORCE_TENANTS", "").split(",") if t.strip()}
    if tenants:
        return "enforce" if str(getattr(job, "tenant_id", "")) in tenants else "observe"
    return "enforce" if str(getattr(job, "workload_class", "") or "").lower() == "batch" else "observe"


def _content_key(document, job, official: str, pairs: dict) -> str:
    payload = json.dumps({
        "s": [(s.get("text"), s.get("start"), s.get("end"), s.get("segment_id"), s.get("qa_dismissed"))
              for s in (document.current_segments or []) if isinstance(s, dict)],
        "e": (document.machine_evidence or {}).get("evidence_sha256")
        if isinstance(document.machine_evidence, dict) else None,
        "o": hashlib.sha1((official or "").encode()).hexdigest(),
        "m": sorted(pairs.items()),
        "t": str(getattr(job, "song_title", "") or ""),
        "n": len(document.original_segments or []),
    }, sort_keys=True, default=str)
    return hashlib.sha1(payload.encode()).hexdigest()


def review_for_document(db, document, job=None) -> dict:
    """Revisión rápida de la letra actual del documento. Nunca rompe la
    transacción del request: las lecturas van en un savepoint."""
    job_mode = effective_mode(job)
    if job_mode == "off":
        return {"schema": "lyric-review-v1", "mode": "off", "items": [],
                "required_count": 0, "suggested_count": 0, "sources": {}, "risk": {}}
    try:
        if job is None:
            from database import Job
            job = db.query(Job).filter(Job.job_id == document.job_id).first()
            job_mode = effective_mode(job)
        nested = db.begin_nested() if hasattr(db, "begin_nested") else None
        try:
            text, origin = official_text(db, job) if job is not None else ("", "")
            pairs = memory_pairs(db, job) if job is not None else {}
        except Exception:
            if nested is not None and nested.is_active:
                nested.rollback()
            raise
        if nested is not None and nested.is_active:
            nested.commit()
        if job is not None and not text:
            schedule_official_fetch(job)
        key = _content_key(document, job, text, pairs)
        cached = _review_cache.get_fresh(key)
        if cached is None:
            cached = build_review(
                list(document.current_segments or []),
                original_segments=document.original_segments,
                machine_evidence=document.machine_evidence,
                title=str(getattr(job, "song_title", "") or ""),
                official_text=text,
                memory_pairs=pairs,
            )
            cached["sources"]["official_origin"] = origin
            _review_cache.put(key, cached)
        return {**cached, "mode": job_mode}
    except Exception:  # pragma: no cover - un detector nunca tumba el editor
        logger.exception("[LYRIC-REVIEW] review failed job=%s", getattr(document, "job_id", None))
        return {"schema": "lyric-review-v1", "mode": job_mode, "items": [],
                "required_count": 0, "suggested_count": 0, "sources": {}, "risk": {},
                "error": "unavailable"}


def pending_for(db, document, job=None, *, scope: str = "full") -> LyricReviewPending | None:
    """El error a levantar si la aprobación debe esperar; ``None`` si no."""
    if scope == "none" or effective_mode(job) != "enforce":
        return None
    review = review_for_document(db, document, job)
    pending = required_for_scope(review, scope)
    return LyricReviewPending(review, pending) if pending else None


def require_resolved(db, document, job=None, *, scope: str = "full") -> None:
    error = pending_for(db, document, job, scope=scope)
    if error is not None:
        raise error
