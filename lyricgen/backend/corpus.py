"""Gold corpus annotation tool — validator calibration project.

Genly needs a 50-song corpus with human "gold" ground truth (exact phrase
boundaries + lexical/vocalization/mixed classification) to calibrate the
automatic transcription-quality validator before trusting it unsupervised.
Two non-technical annotators mark each song BLIND and INDEPENDENTLY; an
admin later adjudicates the differences by hand.

This module is intentionally a separate surface from the production
LyricsEditor/editor.py stack:
  - It has its own tables (CorpusSong / CorpusAnnotatorToken /
    CorpusAnnotation) — no `jobs` row is created or touched.
  - Annotators authenticate with a long random token embedded in the URL
    (`/annotate/{token}`), never a username/password/JWT. There is no
    login screen; the link itself is the credential.
  - Every token-scoped endpoint below resolves the annotator's identity
    from the `token` path parameter and filters ALL queries by
    `annotator_token_id` derived from it. No endpoint in this router
    accepts an annotator id as an argument, so there is no code path
    where annotator A's request can read or write annotator B's row for
    the same song — that is how the "blind" requirement is enforced.
    The only place both annotators' data is shown together is the
    admin-only comparison endpoint at the bottom of this file.
"""

import logging
import secrets
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import storage
from admin import require_admin
from database import (
    AuditLog,
    CorpusAnnotation,
    CorpusAnnotatorToken,
    CorpusSong,
    Job,
    get_db,
    utcnow,
)

logger = logging.getLogger("genly.corpus")

router = APIRouter(tags=["corpus"])

VALID_EVENT_TYPES = {"lexical", "vocalization", "mixed"}
VALID_STATUSES = {"draft", "submitted"}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _resolve_annotator(token: str, db: Session) -> CorpusAnnotatorToken:
    """Look up the annotator identity from the URL token. 404s (not 401/403)
    so a wrong/typo'd link doesn't hint at whether the token format is
    merely inactive vs entirely unknown — both look identical to the caller."""
    row = (
        db.query(CorpusAnnotatorToken)
        .filter(CorpusAnnotatorToken.token == token)
        .first()
    )
    if row is None or not row.is_active:
        raise HTTPException(status_code=404, detail="Link no válido.")
    return row


def _touch_annotator(annotator: CorpusAnnotatorToken, db: Session) -> None:
    annotator.last_used_at = utcnow()
    db.commit()


def _validate_segments(segments: list) -> list:
    if not isinstance(segments, list):
        raise HTTPException(status_code=422, detail="segments debe ser una lista.")
    cleaned = []
    for i, seg in enumerate(segments):
        if not isinstance(seg, dict):
            raise HTTPException(status_code=422, detail=f"Segmento {i} inválido.")
        try:
            start = float(seg.get("start"))
            end = float(seg.get("end"))
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=422, detail=f"Segmento {i}: start/end deben ser números.",
            )
        if end < start:
            raise HTTPException(
                status_code=422, detail=f"Segmento {i}: el fin es anterior al inicio.",
            )
        event_type = seg.get("event_type")
        if event_type not in VALID_EVENT_TYPES:
            raise HTTPException(
                status_code=422,
                detail=f"Segmento {i}: tipo inválido (usar lexical/vocalization/mixed).",
            )
        cleaned.append({
            "start": start,
            "end": end,
            "text": str(seg.get("text") or "").strip(),
            "event_type": event_type,
        })
    return cleaned


# ---------------------------------------------------------------------------
# Admin: songs
# ---------------------------------------------------------------------------

class CreateSongRequest(BaseModel):
    artist: str
    title: str
    # Either point directly at an existing R2 key, or reuse the audio
    # already uploaded for an existing job (copies job.input_r2_key so the
    # corpus never re-uploads audio the platform already has).
    audio_r2_key: Optional[str] = None
    source_job_id: Optional[str] = None
    notes: Optional[str] = None


@router.post("/admin/corpus/songs")
async def create_corpus_song(
    body: CreateSongRequest,
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    audio_r2_key = body.audio_r2_key
    if not audio_r2_key and body.source_job_id:
        job = db.query(Job).filter(Job.job_id == body.source_job_id).first()
        if job is None:
            raise HTTPException(status_code=404, detail="source_job_id no encontrado.")
        if not job.input_r2_key:
            raise HTTPException(
                status_code=422,
                detail="Ese job no tiene audio original (input_r2_key) disponible.",
            )
        audio_r2_key = job.input_r2_key
    if not audio_r2_key:
        raise HTTPException(
            status_code=422,
            detail="Falta audio_r2_key o source_job_id.",
        )

    song = CorpusSong(
        artist=body.artist.strip(),
        title=body.title.strip(),
        audio_r2_key=audio_r2_key,
        notes=body.notes,
        created_by=admin.get("id"),
    )
    db.add(song)
    db.commit()
    db.refresh(song)

    db.add(AuditLog(
        user_id=admin.get("id"),
        action="corpus.song_created",
        detail={"song_id": song.id, "artist": song.artist, "title": song.title},
    ))
    db.commit()

    return song.to_dict()


@router.get("/admin/corpus/songs")
async def list_corpus_songs(
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    songs = (
        db.query(CorpusSong)
        .order_by(CorpusSong.created_at.desc())
        .all()
    )
    return {"songs": [s.to_dict() for s in songs]}


# ---------------------------------------------------------------------------
# Admin: annotator tokens
# ---------------------------------------------------------------------------

class CreateAnnotatorRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)


@router.post("/admin/corpus/annotators")
async def create_annotator(
    body: CreateAnnotatorRequest,
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Generates one magic link per annotator. `token` is 256 bits of
    randomness (secrets.token_urlsafe) — unguessable, never expires, and
    IS the authentication. The frontend builds the shareable URL as
    `{origin}/annotate/{token}`; this endpoint returns the raw token plus
    a ready-to-send `annotate_path` so the caller doesn't have to know the
    route shape."""
    token = secrets.token_urlsafe(32)
    row = CorpusAnnotatorToken(
        name=body.name.strip(),
        token=token,
        created_by=admin.get("id"),
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    db.add(AuditLog(
        user_id=admin.get("id"),
        action="corpus.annotator_created",
        detail={"annotator_id": row.id, "name": row.name},
    ))
    db.commit()

    payload = row.to_dict()
    payload["annotate_path"] = f"/annotate/{token}"
    return payload


@router.get("/admin/corpus/annotators")
async def list_annotators(
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(CorpusAnnotatorToken)
        .order_by(CorpusAnnotatorToken.created_at.desc())
        .all()
    )
    out = []
    for row in rows:
        d = row.to_dict()
        d["annotate_path"] = f"/annotate/{row.token}"
        submitted = (
            db.query(CorpusAnnotation)
            .filter(
                CorpusAnnotation.annotator_token_id == row.id,
                CorpusAnnotation.status == "submitted",
            )
            .count()
        )
        d["submitted_count"] = submitted
        out.append(d)
    return {"annotators": out}


class UpdateAnnotatorRequest(BaseModel):
    is_active: bool


@router.patch("/admin/corpus/annotators/{annotator_id}")
async def update_annotator(
    annotator_id: int,
    body: UpdateAnnotatorRequest,
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Revoke/restore access. There is no rotate-token affordance on
    purpose — revoking is enough (annotators are told the link is dead),
    and issuing a NEW annotator row keeps the annotation history clean if
    the same person needs a fresh link later."""
    row = db.query(CorpusAnnotatorToken).filter(CorpusAnnotatorToken.id == annotator_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Anotador no encontrado.")
    row.is_active = body.is_active
    db.commit()
    return row.to_dict()


# ---------------------------------------------------------------------------
# Admin: adjudication view (both annotators side by side, per song)
# ---------------------------------------------------------------------------

@router.get("/admin/corpus/songs/{song_id}/annotations")
async def compare_song_annotations(
    song_id: int,
    admin: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """The ONLY endpoint that shows more than one annotator's work for the
    same song — deliberately admin-only, for manual adjudication of the
    differences. Annotator-facing endpoints never do this."""
    song = db.query(CorpusSong).filter(CorpusSong.id == song_id).first()
    if song is None:
        raise HTTPException(status_code=404, detail="Canción no encontrada.")

    rows = (
        db.query(CorpusAnnotation, CorpusAnnotatorToken)
        .join(CorpusAnnotatorToken, CorpusAnnotation.annotator_token_id == CorpusAnnotatorToken.id)
        .filter(CorpusAnnotation.song_id == song_id)
        .all()
    )
    annotations = []
    for annotation, annotator in rows:
        d = annotation.to_dict()
        d["annotator_name"] = annotator.name
        d["annotator_id"] = annotator.id
        annotations.append(d)

    return {"song": song.to_dict(), "annotations": annotations}


# ---------------------------------------------------------------------------
# Annotator-facing (token in the URL, no login)
# ---------------------------------------------------------------------------

@router.get("/annotate/{token}")
async def get_annotator_identity(token: str, db: Session = Depends(get_db)):
    """Cheap existence/name check the frontend uses to greet the annotator
    ("Hola, {name}") and to fail fast with a friendly message on a bad link,
    before it tries to load the song list."""
    annotator = _resolve_annotator(token, db)
    return {"name": annotator.name}


@router.get("/annotate/{token}/songs")
async def list_assigned_songs(token: str, db: Session = Depends(get_db)):
    """All active corpus songs, annotated with THIS annotator's own
    progress only. Every annotator is assigned the full active corpus —
    there is no per-song assignment table — which is the right shape for
    a blind double-annotation project where every song needs exactly two
    independent passes."""
    annotator = _resolve_annotator(token, db)
    songs = (
        db.query(CorpusSong)
        .filter(CorpusSong.is_active.is_(True))
        .order_by(CorpusSong.id.asc())
        .all()
    )
    own_annotations = {
        a.song_id: a
        for a in db.query(CorpusAnnotation).filter(
            CorpusAnnotation.annotator_token_id == annotator.id,
        )
    }
    out = []
    for song in songs:
        d = song.to_dict()
        own = own_annotations.get(song.id)
        d["my_status"] = own.status if own else "not_started"
        d["my_segment_count"] = len(own.segments or []) if own else 0
        out.append(d)
    return {"annotator_name": annotator.name, "songs": out}


def _get_or_create_own_annotation(
    annotator: CorpusAnnotatorToken, song: CorpusSong, db: Session,
) -> CorpusAnnotation:
    row = (
        db.query(CorpusAnnotation)
        .filter(
            CorpusAnnotation.song_id == song.id,
            CorpusAnnotation.annotator_token_id == annotator.id,
        )
        .first()
    )
    if row is None:
        row = CorpusAnnotation(
            song_id=song.id, annotator_token_id=annotator.id, segments=[],
        )
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def _get_song_or_404(song_id: int, db: Session) -> CorpusSong:
    song = db.query(CorpusSong).filter(CorpusSong.id == song_id).first()
    if song is None or not song.is_active:
        raise HTTPException(status_code=404, detail="Canción no encontrada.")
    return song


@router.get("/annotate/{token}/songs/{song_id}")
async def get_own_annotation(token: str, song_id: int, db: Session = Depends(get_db)):
    """Load THIS annotator's own draft (or a fresh empty one) for a song.
    Never touches, and has no way to reach, another annotator's row for
    the same song — see module docstring."""
    annotator = _resolve_annotator(token, db)
    song = _get_song_or_404(song_id, db)
    annotation = _get_or_create_own_annotation(annotator, song, db)
    _touch_annotator(annotator, db)
    return {"song": song.to_dict(), "annotation": annotation.to_dict()}


class SaveAnnotationRequest(BaseModel):
    segments: list = Field(default_factory=list)


@router.put("/annotate/{token}/songs/{song_id}")
async def save_own_annotation(
    token: str, song_id: int, body: SaveAnnotationRequest, db: Session = Depends(get_db),
):
    """Autosave draft. Idempotent — the frontend calls this on every pause
    in editing; it never flips status away from `submitted` on its own
    (only POST .../submit does), so a post-submit tweak stays visible as
    submitted rather than silently reverting to draft."""
    annotator = _resolve_annotator(token, db)
    song = _get_song_or_404(song_id, db)
    annotation = _get_or_create_own_annotation(annotator, song, db)
    annotation.segments = _validate_segments(body.segments)
    db.commit()
    db.refresh(annotation)
    _touch_annotator(annotator, db)
    return annotation.to_dict()


@router.post("/annotate/{token}/songs/{song_id}/submit")
async def submit_own_annotation(token: str, song_id: int, db: Session = Depends(get_db)):
    """Marks the song done. Re-submittable on purpose (a non-technical
    annotator who notices a typo after submitting should be able to fix it
    and hit submit again, not be locked out)."""
    annotator = _resolve_annotator(token, db)
    song = _get_song_or_404(song_id, db)
    annotation = _get_or_create_own_annotation(annotator, song, db)
    if not annotation.segments:
        raise HTTPException(
            status_code=422,
            detail="No se puede enviar una canción sin ninguna frase marcada.",
        )
    annotation.status = "submitted"
    annotation.submitted_at = utcnow()
    db.commit()
    db.refresh(annotation)
    _touch_annotator(annotator, db)
    return annotation.to_dict()


# ---------------------------------------------------------------------------
# Annotator-facing: audio + waveform (reuses the platform's existing R2
# signed-URL + peak-envelope mechanism — see main.get_source_audio_url /
# main.get_waveform for the job-scoped equivalent).
# ---------------------------------------------------------------------------

@router.get("/annotate/{token}/songs/{song_id}/audio-url")
async def get_song_audio_url(token: str, song_id: int, db: Session = Depends(get_db)):
    annotator = _resolve_annotator(token, db)
    song = _get_song_or_404(song_id, db)
    if not storage.object_exists(song.audio_r2_key):
        raise HTTPException(
            status_code=404, detail="El audio de esta canción ya no está disponible.",
        )
    url = storage.generate_signed_url(song.audio_r2_key, expiry_seconds=3600)
    if not url:
        raise HTTPException(status_code=503, detail="Almacenamiento no disponible.")
    _touch_annotator(annotator, db)
    return {"url": url, "expires_in": 3600}


@router.get("/annotate/{token}/songs/{song_id}/waveform")
async def get_song_waveform(token: str, song_id: int, db: Session = Depends(get_db)):
    annotator = _resolve_annotator(token, db)
    song = _get_song_or_404(song_id, db)
    if not storage.is_enabled():
        raise HTTPException(status_code=503, detail="Almacenamiento no disponible.")

    # compute_and_cache_waveform is job-shaped (job_id, input_r2_key) but
    # fully generic underneath — it never touches the Job table, just an
    # id string used to build the R2 cache key. `corpus-<id>` can never
    # collide with a real job_id (those are 12-char uuid4 hex, no dashes).
    from waveform_compute import compute_and_cache_waveform
    payload = compute_and_cache_waveform(f"corpus-{song.id}", song.audio_r2_key)
    if payload is None:
        raise HTTPException(
            status_code=422, detail="No se pudo generar la forma de onda para este audio.",
        )
    _touch_annotator(annotator, db)
    return payload
