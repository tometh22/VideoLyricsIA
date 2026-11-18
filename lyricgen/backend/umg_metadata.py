"""Artista y título canónicos para lo que se entrega a UMG.

POR QUÉ EXISTE
--------------
De 310 pedidos de cambio de UMG (sep-nov 2026), 22 fueron sólo por el
nombre: "Charly Garcia" en vez de "Charly García" (7 veces, en canciones
distintas), "Seru Giran", "Enanitos Verdes" → "Los Enanitos Verdes",
"Tuyo" → "Tuyu", "Dónde Estarás" sin ¿?, "Coti" sin "Ft. Enrique Iglesias"
en la misma canción que ya habían corregido. El nombre sale de la planilla
o del nombre del archivo de la campaña tal cual, se dibuja en la tarjeta del
video y se copia al portal; nada lo comparaba con lo que UMG ya había
pedido. (No había nombres truncados: "Cuando Miro En Tus Ojo" o "Guerrilla
Sexua" eran el recorte del listado de auditoría; el portal tenía el nombre
entero.)

QUÉ HACE
--------
``normalize(artist, title)`` devuelve el nombre canónico:

1. correcciones de una canción puntual (``songs`` del catálogo y las que
   UMG pidió y se aplicaron: ``learned_corrections``);
2. el artista del catálogo versionado ``data/umg_metadata_catalog.json``
   (clave sin tildes, mayúsculas ni espacios), también dentro de un
   "A, B" / "A Ft. B";
3. un artista que UMG corrigió sólo en la escritura (tildes, mayúsculas,
   "Los") se escribe igual en TODAS sus canciones siguientes.

Se aplica al registrar la campaña, al encolar cada render (la tarjeta del
video y el snapshot del portal salen del Job) y, como control, al publicar:
un video renderizado con un nombre no canónico no se publica
(``publication_problem``; ``UMG_METADATA_GATE=observe|off`` lo apaga).
Puro salvo ``learned_corrections`` (lectura).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping

logger = logging.getLogger(__name__)

CATALOG_PATH = Path(__file__).with_name("data") / "umg_metadata_catalog.json"
LEARNED_EVENT = "umg_metadata_corrected"
_LEARNED_TTL_S = 600.0
_ARTICLES = ("los", "las", "la", "el", "the")
_SEPARATOR = re.compile(
    r"(\s*,\s*|\s+&\s+|\s+y\s+|\s+-?\s*(?:ft|feat|featuring)\.?\s+)", re.I)


def key(value: Any) -> str:
    """Clave de comparación: sin tildes, mayúsculas, espacios ni signos."""
    text = unicodedata.normalize("NFKD", str(value or "").lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^0-9a-zñ]+", "", text.replace("ñ", "n"))


def _clean(value: Any) -> str:
    text = unicodedata.normalize("NFC", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    # "Coti - Ft. Enrique Iglesias" (tipeado así al corregir) → "Coti Ft. …"
    return re.sub(r"\s+-\s+(ft|feat)\.?\s+", lambda m: f" {m.group(1).capitalize()}. ",
                  text, flags=re.I)


def _article_key(value: str) -> str:
    words = str(value or "").split()
    if words and key(words[0]) in _ARTICLES:
        return key(" ".join(words[1:]))
    return key(value)


@dataclass(frozen=True)
class Catalog:
    version: str
    artists: Mapping[str, str]
    songs: Mapping[tuple[str, str], Mapping[str, str]]


@lru_cache(maxsize=1)
def load_catalog(path: str | None = None) -> Catalog:
    with open(path or CATALOG_PATH, encoding="utf-8") as fh:
        raw = json.load(fh)
    artists: dict[str, str] = {}
    for row in raw.get("artists") or []:
        name = unicodedata.normalize("NFC", str(row["name"]).strip())
        for alias in [name, *(row.get("aliases") or [])]:
            artists[key(alias)] = name
    songs: dict[tuple[str, str], dict[str, str]] = {}
    for row in raw.get("songs") or []:
        names = row["artist"] if isinstance(row["artist"], list) else [row["artist"]]
        patch = {k: unicodedata.normalize("NFC", row[f"set_{k}"])
                 for k in ("artist", "title") if row.get(f"set_{k}")}
        for name in names:
            for artist_key in {key(name), key(artists.get(key(name), name))}:
                songs[(artist_key, key(row["title"]))] = patch
    return Catalog(version=str(raw.get("version") or ""), artists=artists, songs=songs)


@dataclass(frozen=True)
class Learned:
    artists: Mapping[str, str] = field(default_factory=dict)
    songs: Mapping[tuple[str, str], Mapping[str, str]] = field(default_factory=dict)


@dataclass(frozen=True)
class MetadataResult:
    artist: str
    title: str
    changes: tuple[dict, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.changes)


def _canonical_artist(name: str, catalog: Catalog, learned: Learned) -> str:
    whole = key(name)
    if whole in catalog.artists:
        return catalog.artists[whole]
    if whole in learned.artists:
        return learned.artists[whole]
    parts = _SEPARATOR.split(name)
    if len(parts) == 1:
        return name
    out = []
    for index, part in enumerate(parts):
        if index % 2:  # separador
            sep = part
            if re.search(r"ft|feat", sep, re.I) and not re.search(r"featuring", sep, re.I):
                word = re.search(r"ft|feat", sep, re.I).group(0)
                sep = f" {word[:1].upper()}{word[1:].lower()}. "
            out.append(sep)
            continue
        k = key(part)
        out.append(catalog.artists.get(k) or learned.artists.get(k) or part)
    return "".join(out)


def _reason(clean: str, new: str, kind: str) -> str:
    """``whitespace``/``case``: sólo espacios o mayúsculas (la tarjeta del
    video pone el artista en mayúsculas igual); si no, ``kind``."""
    if clean == new:
        return "whitespace"
    if clean.lower() == new.lower():
        return "case"
    return kind


def normalize(artist: Any, title: Any, *, learned: Learned | None = None,
              catalog: Catalog | None = None) -> MetadataResult:
    """Nombre canónico de una entrega a UMG. Nunca inventa: sin coincidencia
    en el catálogo o en lo aprendido, devuelve el mismo nombre (limpio de
    espacios dobles)."""
    catalog = catalog or load_catalog()
    learned = learned or Learned()
    raw_artist, raw_title = str(artist or ""), str(title or "")
    clean_artist, clean_title = _clean(raw_artist), _clean(raw_title)
    canonical = _canonical_artist(clean_artist, catalog, learned) if clean_artist else ""
    title_key = key(clean_title)
    patch: Mapping[str, str] = {}
    for artist_key in (key(clean_artist), key(canonical)):
        patch = catalog.songs.get((artist_key, title_key)) or learned.songs.get(
            (artist_key, title_key)) or {}
        if patch:
            break
    new_artist = patch.get("artist") or canonical
    if patch.get("artist"):
        new_artist = _canonical_artist(_clean(new_artist), catalog, learned)
    new_title = patch.get("title") or clean_title
    changes = []
    if new_artist != raw_artist and new_artist:
        changes.append({"field": "artist", "from": raw_artist, "to": new_artist,
                        "reason": _reason(clean_artist, new_artist,
                                          "song" if patch.get("artist") else "catalog")})
    if new_title != raw_title and new_title:
        changes.append({"field": "song_title", "from": raw_title, "to": new_title,
                        "reason": _reason(clean_title, new_title, "song")})
    return MetadataResult(artist=new_artist or raw_artist, title=new_title or raw_title,
                          changes=tuple(changes))


# --------------------------------------------------------------------------
# Lo que UMG ya pidió y se aplicó: se recuerda para las canciones siguientes.
# --------------------------------------------------------------------------

def _recase(new: str, old: str) -> str:
    """UMG suele escribir el pedido en mayúsculas ("LOS ENANITOS VERDES",
    "CHARLY GARCÍA"): se toma su ortografía, no su caja."""
    letters = [c for c in new if c.isalpha()]
    if not letters or not (all(c.isupper() for c in letters) or all(c.islower() for c in letters)):
        return new
    old_words = {key(w): w for w in str(old or "").split()}
    out = []
    for word in new.split():
        prior = old_words.get(key(word))
        if prior is not None and prior.isupper():
            out.append(word.upper())
        elif prior is not None and len(prior) == len(word) and not prior.islower():
            out.append("".join(a.upper() if b.isupper() else a.lower() for a, b in zip(word, prior)))
        else:
            out.append(word[:1].upper() + word[1:].lower())
    return " ".join(out)


def _learn(learned_artists: dict, learned_songs: dict, *, field_name: str, before: str,
           after: str, artist: str, title: str) -> None:
    before, after = _clean(before), _clean(after)
    if not before or not after or before == after:
        return
    if field_name == "artist":
        value = _recase(after, before)
        if key(before) == key(after) or _article_key(before) == _article_key(after):
            learned_artists[key(before)] = value
            learned_artists[key(after)] = value
        else:
            learned_songs[(key(before), key(title))] = {"artist": value}
    elif field_name == "song_title":
        value = _recase(after, before)
        for artist_key in {key(artist)}:
            learned_songs[(artist_key, key(before))] = {"title": value}
            learned_songs[(artist_key, key(after))] = {"title": value}


_learned_lock = threading.Lock()
_learned_cache: dict[str, tuple[float, Learned]] = {}


def learned_corrections(db, *, now: float | None = None) -> Learned:
    """Correcciones de artista/título aplicadas por pedido de UMG, de la más
    vieja a la más nueva (la última gana). Fuentes: propuestas de pedidos de
    cambio aplicadas (``set_metadata``) y ediciones de metadatos de un video
    de UMG (evento ``umg_metadata_corrected``). Si falla la lectura, vacío."""
    now = time.monotonic() if now is None else now
    with _learned_lock:
        hit = _learned_cache.get("all")
        if hit and now - hit[0] < _LEARNED_TTL_S:
            return hit[1]
    artists: dict[str, str] = {}
    songs: dict[tuple[str, str], dict[str, str]] = {}
    rows: list[tuple[Any, str, str, str, str, str]] = []
    try:
        from database import ChangeRequestProposal, Job, ProductEvent

        nested = db.begin_nested() if hasattr(db, "begin_nested") else None
        try:
            proposals = (
                db.query(ChangeRequestProposal.applied_at, ChangeRequestProposal.operations,
                         Job.artist, Job.song_title)
                .join(Job, Job.job_id == ChangeRequestProposal.job_id)
                .filter(ChangeRequestProposal.status.in_(("applied", "partially_applied")))
                .order_by(ChangeRequestProposal.applied_at.desc())
                .limit(2000).all()
            )
            for applied_at, operations, artist, title in proposals:
                for op in operations or []:
                    if not isinstance(op, dict) or op.get("kind") != "set_metadata":
                        continue
                    if op.get("status") != "applied":
                        continue
                    if op.get("field") not in {"artist", "song_title"}:
                        continue
                    rows.append((applied_at, op["field"], str(op.get("current_value") or ""),
                                 str(op.get("proposed_value") or ""), str(artist or ""),
                                 str(title or "")))
            events = (
                db.query(ProductEvent.created_at, ProductEvent.properties)
                .filter(ProductEvent.name == LEARNED_EVENT)
                .order_by(ProductEvent.created_at.desc()).limit(2000).all()
            )
            for created_at, props in events:
                if not isinstance(props, dict):
                    continue
                rows.append((created_at, str(props.get("field") or ""),
                             str(props.get("before") or ""), str(props.get("after") or ""),
                             str(props.get("artist") or ""), str(props.get("title") or "")))
        finally:
            if nested is not None and nested.is_active:
                nested.rollback()
    except Exception:  # pragma: no cover - aprender nunca frena un render
        logger.warning("[UMG-METADATA] learned corrections unavailable", exc_info=True)
        return Learned()
    rows.sort(key=lambda r: (r[0] is None, r[0]))
    for _, field_name, before, after, artist, title in rows:
        _learn(artists, songs, field_name=field_name, before=before, after=after,
               artist=artist, title=title)
    learned = Learned(artists=artists, songs=songs)
    with _learned_lock:
        _learned_cache["all"] = (now, learned)
    return learned


def invalidate_learned() -> None:
    with _learned_lock:
        _learned_cache.clear()


def record_correction(db, job, *, field_name: str, before: str, after: str) -> None:
    """Deja asentada una corrección de metadatos de un video de UMG para que
    la próxima canción del mismo artista ya salga bien."""
    if field_name not in {"artist", "song_title"} or _clean(before) == _clean(after):
        return
    try:
        from database import ProductEvent

        db.add(ProductEvent(
            tenant_id=str(getattr(job, "tenant_id", "") or ""),
            job_id=getattr(job, "job_id", None),
            name=LEARNED_EVENT,
            properties={
                "field": field_name, "before": before, "after": after,
                "artist": str(getattr(job, "artist", "") or ""),
                "title": str(getattr(job, "song_title", "") or ""),
                "catalog_version": load_catalog().version,
            },
        ))
        invalidate_learned()
    except Exception:  # pragma: no cover
        logger.warning("[UMG-METADATA] could not record correction job=%s",
                       getattr(job, "job_id", None), exc_info=True)


# --------------------------------------------------------------------------
# Dónde se aplica.
# --------------------------------------------------------------------------

def applies_to_job(job) -> bool:
    """Videos de campañas o con entrega UMG; el resto de los tenants no."""
    def text(name: str) -> str:
        value = getattr(job, name, None)
        return value if isinstance(value, str) else ""

    return bool(
        text("campaign_id")
        or text("workload_class") == "batch"
        or text("delivery_profile").lower() in {"umg", "both"}
    )


def normalize_for_db(db, artist: Any, title: Any) -> MetadataResult:
    return normalize(artist, title, learned=learned_corrections(db))


def normalize_job(db, job) -> MetadataResult | None:
    """Pone en el Job el nombre canónico antes de renderizar. Devuelve el
    resultado si cambió algo."""
    if not applies_to_job(job) or not isinstance(getattr(job, "artist", None), str):
        return None
    result = normalize_for_db(db, getattr(job, "artist", ""), getattr(job, "song_title", ""))
    if not result.changed:
        return None
    job.artist = result.artist
    job.song_title = result.title or None
    logger.info("[UMG-METADATA] job=%s %s", getattr(job, "job_id", None),
                "; ".join(f"{c['field']}: {c['from']!r} -> {c['to']!r}" for c in result.changes))
    return result


def gate_mode() -> str:
    value = os.environ.get("UMG_METADATA_GATE", "enforce").strip().lower()
    return value if value in {"enforce", "observe", "off"} else "enforce"


def publication_problem(db, job) -> dict | None:
    """``None`` si el artista y el título del video son los canónicos; si
    no, el detalle para frenar la publicación (el video ya tiene dibujado el
    nombre viejo en la tarjeta: hay que corregir y volver a renderizar)."""
    mode = gate_mode()
    if mode == "off" or not isinstance(getattr(job, "artist", None), str):
        return None
    result = normalize_for_db(db, getattr(job, "artist", ""), getattr(job, "song_title", ""))
    # Espacios de más o sólo mayúsculas: se corrigen en el próximo render,
    # pero no frenan una publicación (UMG nunca lo reclamó).
    significant = [c for c in result.changes if c["reason"] not in {"whitespace", "case"}]
    if not significant:
        return None
    expected = {c["field"]: c["to"] for c in significant}
    parts = []
    if "artist" in expected:
        parts.append(f"artista «{expected['artist']}»")
    if "song_title" in expected:
        parts.append(f"título «{expected['song_title']}»")
    detail = {
        "code": "metadata_not_canonical",
        "blocked": mode == "enforce",
        "artist": getattr(job, "artist", ""),
        "song_title": getattr(job, "song_title", ""),
        "expected_artist": result.artist,
        "expected_song_title": result.title,
        "changes": significant,
        "message": (
            "El nombre no es el que pidió UMG: corregí " + " y ".join(parts)
            + " (Editar → artista/título) y volvé a renderizar antes de publicar."
        ),
    }
    if mode == "observe":
        logger.warning("[UMG-METADATA] observe job=%s %s", getattr(job, "job_id", None),
                       detail["message"])
        return None
    return detail


def matches_request(current: Any, requested: Any) -> bool:
    """Un pedido "Corregir artista a: LOS ENANITOS VERDES" ya está cumplido
    si el video dice el nombre canónico de lo pedido."""
    return _clean(current) == normalize(requested, "").artist


def iter_changes(results: Iterable[MetadataResult]) -> list[dict]:
    return [change for result in results for change in result.changes]
