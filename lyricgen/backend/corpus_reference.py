"""Reference-segment precarga for the gold corpus (see corpus.py).

37 of the 50 corpus songs were copied from a job whose transcription was
already reviewed and approved by a human in production, and lives in
`editor_documents.current_segments`. Without this module, an annotator
opening one of those songs starts from silence and has to mark every
phrase by ear from zero — pure waste when a reviewed starting point
already exists. This module:

  - detects the 6 songs deliberately held out as a blind control (marked
    "CONTROL:" in `CorpusSong.notes`) that must NEVER get a precarga —
    they are the check that annotators do just as well from zero as they
    do reviewing a precarga;
  - converts `editor_documents.current_segments` down to the
    `{start, end, text, event_type}` shape `CorpusAnnotation.segments`
    already uses, discarding renderer/pipeline metadata (`words`,
    `ctc_lr`, `provider_evidence`, `timing_provenance`, `locked`,
    `review`, `pos`, `scale`, `rot`, ...) that means nothing to the
    annotation task. `event_type` always defaults to "lexical" —
    production transcripts carry no lexical/vocalization/mixed
    classification; the annotator adjusts the exceptions by ear instead
    of starting blank on every line;
  - links a CorpusSong back to the job it was copied from purely via
    `audio_r2_key` == `jobs.input_r2_key`. CorpusSong has no persisted
    job pointer: POST /admin/corpus/songs only uses `source_job_id`
    transiently, to resolve the R2 key at creation time (see
    create_corpus_song in corpus.py) — it never stores the id. Since
    `audio_r2_key` was copied byte-for-byte from that job's
    `input_r2_key` at creation time, it IS the de-facto foreign key, one
    join away.

The actual backfill (`backfill_reference_segments`) is idempotent and
safe to re-run: control detection is pure notes-text, and the reference
lookup is a pure join + pure transform — re-running it always recomputes
the same result. Entry points: scripts/backfill_corpus_reference_segments.py
(CLI, run once after deploying this migration) and the admin
POST /admin/corpus/songs/backfill-references endpoint (no shell access
required).
"""

import logging
from typing import Optional

from sqlalchemy.orm import Session

from database import CorpusSong, EditorDocument, Job

logger = logging.getLogger("genly.corpus.reference")

CONTROL_MARKER = "CONTROL:"


def is_control_song(notes: Optional[str]) -> bool:
    """The 6 corpus songs an admin has hand-marked as the blind control
    group by putting the literal string "CONTROL:" somewhere in `notes`."""
    return bool(notes) and CONTROL_MARKER in notes


def clean_reference_segments(current_segments) -> list[dict]:
    """Strip an editor_documents.current_segments payload down to the
    annotation shape. Segments that fail to parse (missing/garbage
    start/end/text) are skipped rather than raising — this feeds a
    best-effort backfill, not a user-facing save path, so one malformed
    row in old data shouldn't blank out an otherwise-usable song."""
    cleaned: list[dict] = []
    if not isinstance(current_segments, list):
        return cleaned
    for seg in current_segments:
        if not isinstance(seg, dict):
            continue
        try:
            start = float(seg.get("start"))
            end = float(seg.get("end"))
        except (TypeError, ValueError):
            continue
        if end < start:
            continue
        text = seg.get("text")
        if text is None:
            text = ""
        cleaned.append({
            "start": start,
            "end": end,
            "text": str(text).strip(),
            "event_type": "lexical",
        })
    return cleaned


def _find_source_job(song: CorpusSong, db: Session) -> Optional[Job]:
    """Best-effort match of the delivered job a corpus song was copied
    from. Most-recently-created wins on the rare chance more than one job
    shares the exact same input_r2_key."""
    return (
        db.query(Job)
        .filter(Job.input_r2_key == song.audio_r2_key)
        .order_by(Job.id.desc())
        .first()
    )


def backfill_reference_segments(db: Session, *, dry_run: bool = False) -> dict:
    """Idempotent one-off over the whole corpus (control or not, active or
    not — a deactivated song can still get its reference precomputed).
    Returns a summary dict; when dry_run=True nothing is persisted."""
    songs = db.query(CorpusSong).all()
    stats = {
        "total": len(songs),
        "control": 0,
        "seeded": 0,
        "no_job_match": 0,
        "no_editor_document": 0,
        "empty_segments": 0,
    }
    for song in songs:
        if is_control_song(song.notes):
            stats["control"] += 1
            song.is_control = True
            # Non-negotiable: control songs never carry a precarga, even
            # if a matching editor_documents row exists.
            song.reference_segments = None
            continue

        job = _find_source_job(song, db)
        if job is None:
            stats["no_job_match"] += 1
            continue

        document = (
            db.query(EditorDocument).filter(EditorDocument.job_id == job.job_id).first()
        )
        if document is None:
            stats["no_editor_document"] += 1
            continue

        cleaned = clean_reference_segments(document.current_segments)
        if not cleaned:
            stats["empty_segments"] += 1
            continue

        song.reference_segments = cleaned
        stats["seeded"] += 1

    if dry_run:
        db.rollback()
    else:
        db.commit()
    return stats
