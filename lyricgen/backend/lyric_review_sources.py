"""Fuentes con base de datos para la revisión rápida (``lyric_review``).

- Letra oficial: la que pegó el operador para esta canción, la de la
  planilla de la campaña o la de lrclib ya cacheada. Si no hay ninguna, se
  busca en lrclib en segundo plano (nunca dentro del request) y aparece en
  el próximo guardado.
- Memoria de correcciones: lo que los revisores ya corrigieron en otras
  canciones del mismo artista (máquina → aprobado), para no volver a
  entregar "chuchu" cuando ya se aprendió que es "Xuxú".
"""
from __future__ import annotations

import difflib
import json
import logging
import os
import threading
import time

from lyric_review import LyricReviewPending, build_review, fold, mode

logger = logging.getLogger(__name__)

_OPERATOR_PREFIX = "ref:"
_MEMORY_TTL_S = 600.0
_memory_cache: dict[str, tuple[float, dict[str, str]]] = {}
_memory_lock = threading.Lock()
_fetching: set[str] = set()
_fetch_attempted: dict[str, float] = {}
_FETCH_RETRY_S = 6 * 3600.0
_fetch_lock = threading.Lock()


def _operator_key(job_id: str) -> str:
    return f"{_OPERATOR_PREFIX}{job_id}"


def official_text(db, job) -> tuple[str, str]:
    """(texto, origen) de la mejor letra oficial disponible sin red."""
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
    artist, title = str(job.artist or ""), str(job.song_title or "")
    if artist and title:
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
    """Busca la letra en lrclib en un hilo aparte; el resultado queda en la
    caché y se usa en el próximo guardado del editor."""
    if os.environ.get("LYRIC_REVIEW_FETCH_OFFICIAL", "1").strip() == "0":
        return
    artist, title = str(job.artist or "").strip(), str(job.song_title or "").strip()
    if not artist or not title:
        return
    key = fold(f"{artist}|{title}")
    now = time.monotonic()
    with _fetch_lock:
        # Una búsqueda por canción cada 6 h: si lrclib no la tiene, abrir el
        # editor no vuelve a salir a la red.
        if key in _fetching or now - _fetch_attempted.get(key, -_FETCH_RETRY_S) < _FETCH_RETRY_S:
            return
        _fetching.add(key)
        _fetch_attempted[key] = now

    def run():
        from database import SessionLocal
        db = SessionLocal()
        try:
            from pipeline import _fetch_lrclib
            _fetch_lrclib(artist, title, db=db)
            db.commit()
        except Exception:  # pragma: no cover - mejor esfuerzo
            db.rollback()
            logger.exception("[LYRIC-REVIEW] official lyrics fetch failed")
        finally:
            db.close()
            with _fetch_lock:
                _fetching.discard(key)

    threading.Thread(target=run, name="lyric-review-official", daemon=True).start()


def _sound(word: str) -> str:
    """Clave fonética gruesa del español rioplatense/chileno."""
    import re as _re
    w = fold(word)
    for a, b in (("ch", "x"), ("sh", "x"), ("qu", "k"), ("ll", "y"), ("v", "b"), ("z", "s"),
                 ("ce", "se"), ("ci", "si"), ("c", "k"), ("h", "")):
        w = w.replace(a, b)
    return _re.sub(r"(.)\1+", r"\1", w)


def mine_correction_pairs(documents: list[tuple[list, list]]) -> dict[str, str]:
    """Pares palabra-de-máquina → palabra-aprobada, uno a uno, de las
    canciones ya aprobadas de un artista. Se descarta un par si la palabra
    "equivocada" sigue apareciendo en alguna letra aprobada: entonces es una
    palabra legítima y la corrección fue de contexto, no de oído."""
    pairs: dict[str, str] = {}
    approved_vocab: set[str] = set()
    raw_pairs: list[tuple[str, str]] = []
    for original, approved in documents:
        orig_raw = [w for s in (original or []) for w in str(s.get("text") or "").split()]
        appr_raw = [w for s in (approved or []) for w in str(s.get("text") or "").split()]
        a = [fold(w).strip(".,;:!?¡¿\"'()") for w in orig_raw]
        b = [fold(w).strip(".,;:!?¡¿\"'()") for w in appr_raw]
        approved_vocab.update(b)
        sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
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
                if difflib.SequenceMatcher(a=_sound(wrong), b=_sound(right)).ratio() < 0.6:
                    continue
                raw_pairs.append((wrong, right_raw.strip(".,;:!?¡¿\"'()")))
    for wrong, right in raw_pairs:
        if wrong in approved_vocab:
            continue
        pairs[wrong] = right
    return pairs


def memory_pairs(db, job) -> dict[str, str]:
    artist = fold(str(job.artist or "")).strip()
    if not artist:
        return {}
    now = time.monotonic()
    with _memory_lock:
        hit = _memory_cache.get(artist)
        if hit and now - hit[0] < _MEMORY_TTL_S:
            return hit[1]
    from database import EditorDocument, EditorVersion, Job

    candidates = db.query(Job.job_id, Job.artist).filter(
        Job.job_id != job.job_id, Job.artist.isnot(None),
    ).filter(Job.artist.ilike(f"%{str(job.artist).strip()[:40]}%")).limit(200).all()
    job_ids = [jid for jid, a in candidates if fold(a or "").strip() == artist][:40]
    documents = []
    for jid in job_ids:
        doc = db.query(EditorDocument.original_segments).filter(EditorDocument.job_id == jid).first()
        ver = db.query(EditorVersion.segments).filter(
            EditorVersion.job_id == jid, EditorVersion.is_approved.is_(True),
        ).order_by(EditorVersion.revision.desc()).first()
        if doc and ver:
            documents.append((doc[0] or [], ver[0] or []))
    pairs = mine_correction_pairs(documents)
    with _memory_lock:
        _memory_cache[artist] = (now, pairs)
    return pairs


def review_for_document(db, document, job=None) -> dict:
    """Revisión rápida de la letra actual del documento. Nunca rompe."""
    if mode() == "off":
        return {"schema": "lyric-review-v1", "mode": "off", "items": [],
                "required_count": 0, "suggested_count": 0, "sources": {}, "risk": {}}
    try:
        if job is None:
            from database import Job
            job = db.query(Job).filter(Job.job_id == document.job_id).first()
        text, origin = official_text(db, job) if job is not None else ("", "")
        if job is not None and not text:
            schedule_official_fetch(job)
        review = build_review(
            list(document.current_segments or []),
            original_segments=document.original_segments,
            machine_evidence=document.machine_evidence,
            title=str(getattr(job, "song_title", "") or ""),
            official_text=text,
            memory_pairs=memory_pairs(db, job) if job is not None else {},
        )
        review["sources"]["official_origin"] = origin
        return review
    except Exception:  # pragma: no cover - un detector nunca tumba el editor
        logger.exception("[LYRIC-REVIEW] review failed job=%s", getattr(document, "job_id", None))
        return {"schema": "lyric-review-v1", "mode": mode(), "items": [],
                "required_count": 0, "suggested_count": 0, "sources": {}, "risk": {},
                "error": "unavailable"}


def require_resolved(db, document, job=None) -> None:
    if mode() != "enforce":
        return
    review = review_for_document(db, document, job)
    if review.get("required_count"):
        raise LyricReviewPending(review)
