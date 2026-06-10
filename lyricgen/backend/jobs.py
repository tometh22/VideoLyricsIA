"""Job management — PostgreSQL backed."""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

import storage
from database import Job, get_db

_logger = logging.getLogger("genly.jobs")

# Postgres on Railway occasionally drops idle connections in ways that
# pool_pre_ping + TCP keepalives don't fully prevent — the drop happens
# AFTER the pre-ping and BEFORE the actual query, a narrow race we can
# only catch by retrying. Markers cover the SQLAlchemy/psycopg2 strings
# we've actually observed in production logs on /upload-part-proxy. Kept
# in sync with main.py:_TRANSIENT_DB_MARKERS — both lists are short, so
# the duplication is cheaper than a new shared module.
_TRANSIENT_DB_MARKERS = (
    "SSL connection has been closed",
    "server closed the connection",
    "connection already closed",
    "could not connect to server",
)


def create_job(
    db: Session,
    artist: str,
    style: str,
    filename: str,
    user_id: int,
    tenant_id: str = "default",
    delivery_profile: str = "youtube",
    umg_spec: Optional[dict] = None,
    initial_status: str = "processing",
    song_title: str = "",
    input_r2_key: Optional[str] = None,
) -> str:
    """Create a new job and return its ID.

    `initial_status` values:
      - "processing": worker is going to pick this up immediately.
      - "queued": waiting in line behind other jobs at the tenant cap.
      - "transcribed_pending": user has called /transcribe; the audio is
        persisted but the user is still editing lyrics. /generate will
        flip the row to processing/queued once the segments come in.
      - "awaiting_upload": browser is still PUTting bytes directly to
        R2 via a presigned URL. /transcribe-uploaded promotes to
        transcribed_pending once the upload completes.
      - "bg_preview_queued": background-preview "ghost" job (Capa C feature
        2026-05-24) — no audio, no transcription, just a tracking row so
        the wizard can poll the pre-render status of the Veo background.
        The worker promotes to "bg_preview_done" / "bg_preview_failed".
    """
    valid_states = (
        "processing", "queued", "transcribed_pending", "awaiting_upload",
        "bg_preview_queued",
    )
    if initial_status not in valid_states:
        raise ValueError(f"unsupported initial_status {initial_status!r}")
    job_id = uuid.uuid4().hex[:12]
    job = Job(
        job_id=job_id,
        user_id=user_id,
        tenant_id=tenant_id,
        artist=artist,
        song_title=song_title or None,
        style=style,
        filename=filename,
        delivery_profile=delivery_profile,
        umg_spec=umg_spec,
        status=initial_status,
        current_step=(
            "whisper" if initial_status == "processing"
            else "queued" if initial_status == "queued"
            else "uploading" if initial_status == "awaiting_upload"
            else "editing"
        ),
        progress=0,
        input_r2_key=input_r2_key,
    )
    db.add(job)
    db.commit()
    return job_id


def get_job(
    db: Session,
    job_id: str,
    *,
    tenant_id: str = None,
    user_id: int = None,
) -> Optional[dict]:
    """Return a job dict or None if not found.

    TENANT-ISOLATION CONTRACT (UMG-launch hardening 2026-06-01):

    - `tenant_id` MUST be passed by every user-facing endpoint — it is
      the isolation boundary between customers. The standard pattern is
      `get_job(db, job_id, **_job_scope(current_user))` (main.py), which
      always supplies it. Omitting it returns the job regardless of
      which tenant owns it.
    - `user_id` SHOULD additionally be passed for self-serve callers — it
      closes the IDOR where many self-registered users land in
      tenant_id="default" (e.g. the admin tenant) and could otherwise see
      each other's jobs by enumerating job_ids.
    - Internal/worker code that legitimately needs an unscoped row read
      should use `get_job_model()` instead, so an unscoped get_job() call
      always means "someone forgot the tenant filter" — we log it loudly
      below.

    Both filters are keyword-only so a future positional call can't
    silently pass tenant_id into the wrong slot.
    """
    if tenant_id is None and user_id is None:
        # Not an exception — admin paths may make legitimate global
        # lookups — but loud enough that an unscoped call added to a
        # user-facing endpoint shows up in logs/review immediately.
        _logger.warning(
            "get_job(%s) called without tenant/user scope — global lookup "
            "(use get_job_model() for intentional internal reads)",
            job_id,
        )
    query = db.query(Job).filter(Job.job_id == job_id)
    if tenant_id:
        query = query.filter(Job.tenant_id == tenant_id)
    if user_id is not None:
        query = query.filter(Job.user_id == user_id)
    job = query.first()
    return job.to_dict() if job else None


def get_job_model(db: Session, job_id: str) -> Optional[Job]:
    """Return the raw Job model instance."""
    return db.query(Job).filter(Job.job_id == job_id).first()


def get_job_model_resilient(
    db: Session, job_id: str, max_attempts: int = 3
) -> Optional[Job]:
    """Same as get_job_model but retries once on transient Postgres SSL drops.

    The global DbTransientRetryMiddleware (main.py) can't recover the
    upload-proxy endpoints because each multipart part body is 8 MB and
    the middleware refuses to buffer anything over 1 MiB for replay.
    The result is a 500 → frontend retry from byte 0 → user sees the
    progress bar fall to ~0% (concurrency=4, all four in-flight parts
    can fail together on one connection drop).

    We do the retry inline, BEFORE reading the body, so no replay is
    needed. On a transient OperationalError we rollback (which marks
    the connection invalid; SQLAlchemy will check out a fresh one) and
    retry the query. Non-transient errors propagate unchanged.

    Observed in prod 2026-05-14:
        sqlalchemy.exc.OperationalError: (psycopg2.OperationalError)
        SSL connection has been closed unexpectedly
    on POST /upload-part-proxy, 5 times across recent uploads.
    """
    last_err: Optional[OperationalError] = None
    for attempt in range(max_attempts):
        try:
            return get_job_model(db, job_id)
        except OperationalError as e:
            if not any(m in str(e) for m in _TRANSIENT_DB_MARKERS):
                raise
            last_err = e
            try:
                db.rollback()
            except OperationalError:
                # Rollback against an already-dead connection can throw
                # the same error. SQLAlchemy still invalidates the conn
                # so the next checkout is fresh — swallow this one.
                pass
    # Exhausted retries — propagate the last transient error so the
    # caller (and global middleware) sees the real cause.
    assert last_err is not None
    raise last_err


def touch_user_activity(db: Session, job: Job) -> None:
    """Bump last_user_activity_at = now(). Caller commits.

    Used as the staleness anchor for find_abandoned_transcribed: any
    authenticated user touch on the job (POST /save-segments, GET /status,
    etc) refreshes the timestamp, so the reaper only barre genuinely
    abandoned sessions instead of slow batch-edit sessions.
    """
    from datetime import datetime, timezone
    job.last_user_activity_at = datetime.now(timezone.utc)


def set_timing_source(job_id: str, source: str) -> None:
    """Record which engine produced a job's lyric timing (forced_align /
    lrclib_synced / gemini_aligner / whisper) for observability. Best-effort
    with its own short-lived session so it never holds the request
    connection or breaks the transcription path on failure.

    SECURITY/CORRECTNESS (audit 2026-05-24):
    - **Job-missing guard**: log a WARNING if no row matches `job_id` so
      a typo or DB inconsistency doesn't silently produce a no-op. Bug D
      (Cosas Mías auto-recovery) showed that a missing log + missing
      write makes orphan `timing_source=NULL` rows indistinguishable
      from a happy job.
    - **Terminal guard**: do NOT overwrite when the job is already in a
      terminal state (`error`, `done`, etc.). A race with the reaper /
      worker can leave a job tagged successful in a row that the reaper
      already marked failed — confusing telemetry forever.
    """
    _logger = logging.getLogger("genly")
    try:
        from database import SessionLocal
        s = SessionLocal()
        try:
            j = s.query(Job).filter(Job.job_id == job_id).first()
            if j is None:
                _logger.warning(
                    "[TIMING] set_timing_source(%s, %s): no job row — possible orphan",
                    job_id, source,
                )
                return
            # Terminal-state guard. Status values declared terminal at this
            # layer; mirror `update_job`'s terminal set so a reaper-killed
            # job stays consistent.
            if getattr(j, "status", None) in ("error", "transcription_failed"):
                _logger.warning(
                    "[TIMING] set_timing_source(%s, %s) skipped — job is in terminal state %r",
                    job_id, source, j.status,
                )
                return
            j.timing_source = source
            s.commit()
        finally:
            s.close()
    except Exception as e:
        # Surface set-timing failures as warning, not debug — bug D
        # taught us that silent debug is the same as no log at all.
        _logger.warning("[TIMING] set_timing_source failed for %s: %s", job_id, e)


# Estados terminales que cuentan como "intento fallido" para el archivado.
# `done` y `pending_review` quedan AFUERA siempre: son entregables o trabajo
# en revisión — jamás se archivan automáticamente.
_ARCHIVABLE_FAILED_STATUSES = (
    "error", "rejected", "validation_failed", "transcription_failed",
)


def archive_failed_attempts(db: Session, *, keep_job) -> int:
    """Marca archived_at en los intentos FALLIDOS previos del mismo audio.

    Fase 1 (2026-06-10): identidad = user_id + tenant_id + filename exacto.
    (Fase 2 prevista: identidad por hash de contenido del audio, calculado
    por el worker — cubre renombres tipo "Catupecu" vs "Catupecu Machu".)

    Se invoca cuando `keep_job` llega a `done` (aprobación del operador):
    los reintentos fallidos que llevaron a ese éxito dejan de ensuciar la
    historia. A diferencia de supersede_sibling_drafts, acá NO se borra
    nada — audit trail UMG intacto, el cliente los ve con el toggle
    "mostrar archivados".

    Reglas:
      - Solo estados de _ARCHIVABLE_FAILED_STATUSES.
      - Solo intentos ANTERIORES (created_at < keep_job.created_at): un
        fallo posterior al éxito es información nueva, no ruido.
      - Idempotente: archived_at IS NULL filtra los ya archivados.

    Returns: cantidad de filas archivadas. El caller commitea (misma tx
    que la aprobación, así un rollback no deja estados a medias).
    """
    if not keep_job or not keep_job.filename:
        return 0
    now = datetime.now(timezone.utc)
    siblings = (
        db.query(Job)
        .filter(Job.user_id == keep_job.user_id,
                Job.tenant_id == keep_job.tenant_id)
        .filter(Job.job_id != keep_job.job_id)
        .filter(Job.filename == keep_job.filename)
        .filter(Job.status.in_(_ARCHIVABLE_FAILED_STATUSES))
        .filter(Job.archived_at.is_(None))
        .filter(Job.created_at < keep_job.created_at)
        .all()
    )
    for sib in siblings:
        sib.archived_at = now
    if siblings:
        logging.getLogger("genly").info(
            "[ARCHIVE] %s intento(s) fallido(s) archivados bajo %s (%r)",
            len(siblings), keep_job.job_id, keep_job.filename,
        )
    return len(siblings)


def supersede_sibling_drafts(
    db: Session, *, keep_job_id: str, user_id: int, tenant_id: str,
    filename: str, window_min: int = 20,
) -> int:
    """Delete sibling DRAFT jobs (transcribed_pending / awaiting_upload) for
    the same user + same filename created within `window_min`, excluding
    `keep_job_id`.

    Why: the wizard's generate flow can re-upload the audio (new job row)
    instead of reusing the transcribe job, leaving an orphan
    transcribed_pending row that shows up as a phantom "2nd job". This
    removes that orphan at generate time. Time-windowed (default 20 min) so
    it never touches an INTENTIONAL re-upload of the same song hours later.
    Returns the number of rows deleted. Caller need not commit (we do).
    """
    if not filename:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_min)
    siblings = (
        db.query(Job)
        .filter(Job.user_id == user_id, Job.tenant_id == tenant_id)
        .filter(Job.job_id != keep_job_id)
        .filter(Job.filename == filename)
        .filter(Job.status.in_(("transcribed_pending", "awaiting_upload")))
        .filter(Job.created_at >= cutoff)
        .all()
    )
    n = 0
    for sib in siblings:
        db.delete(sib)
        n += 1
    if n:
        db.commit()
        logging.getLogger("genly").info(
            "[DEDUP] superseded %s sibling draft(s) of %s for %r",
            n, keep_job_id, filename,
        )
    return n


def input_audio_is_shared(db: Session, job: Job) -> bool:
    """Return True iff at least one OTHER job in the DB references
    `job.input_r2_key`. Used as a guard before deleting an input audio
    file in R2 — variants and edit-side jobs may share the parent's
    audio key (the upload is done once, then forked).
    Conservative on DB error: returns True (treat as shared, skip delete).
    Losing audio is worse than leaving an orphan in R2 — the
    `cleanup_old_inputs` script (30 d retention) is the long-stop.

    Exposed at module level so the reaper paths (reaper.py) can reuse
    the exact same guard as `_delete_r2_objects` below, instead of
    duplicating the sibling-count logic and drifting.
    """
    if not job.input_r2_key:
        return False
    try:
        others = (
            db.query(Job)
            .filter(Job.input_r2_key == job.input_r2_key)
            .filter(Job.job_id != job.job_id)
            .count()
        )
        return others > 0
    except Exception as exc:
        _logger.warning(
            "Could not count siblings for %s; treating as shared (skip delete): %s",
            job.job_id, exc,
        )
        return True


def _delete_r2_objects(db: Session, job: Job) -> None:
    """Best-effort delete R2 objects tied to a job.

    Output keys (s3_keys: video, short, thumbnail, umg_master, umg_short)
    are per-job and always safe to delete with the row.

    input_r2_key is SHARED across the parent job and every variant/edit
    spawned from it (see main.py:create_variant — variants copy the
    parent's input_r2_key to skip re-upload). Deleting it when a sibling
    still references the same audio breaks every "Reintentar sin
    re-subir" the operator might click on those other jobs, with the
    cryptic "Could not download source audio from R2" message. The
    2026-05-19 agus.cafisi incident on staging — operator deleted one
    failed variant of "Una Vez Más", and the next retry of a sibling
    variant 404'd on R2.

    The fix: count how many OTHER live jobs share the same
    input_r2_key, and skip the R2 delete if any do. The audio gets
    garbage-collected when the last referencing job is deleted. The
    `cleanup_old_inputs` script (retention 30 d) is the long-stop for
    keys whose last-reference deletion was somehow missed.

    Called before the DB row is removed so the reference check counts
    the row we're about to delete as the one self-excluded by job_id.
    Errors are swallowed — R2 cleanup must never block the DB delete.
    """
    keys: list[str] = []
    if job.input_r2_key:
        if not input_audio_is_shared(db, job):
            keys.append(job.input_r2_key)
        else:
            _logger.info(
                "Skipping R2 input delete for %s — sibling job(s) still reference %s",
                job.job_id, job.input_r2_key,
            )
    s3 = job.s3_keys or {}
    if isinstance(s3, dict):
        keys.extend(v for v in s3.values() if isinstance(v, str) and v)
    for key in keys:
        try:
            storage.delete_object(key)
        except Exception as exc:
            _logger.warning("R2 delete failed key=%r: %s", key, exc)


# Estados que el operador puede borrar desde la UI. La idea: cualquier
# estado "en progreso o atascado" es deletable; solo protegemos done /
# pending_review por audit trail + workflow.
#
# Caso real que motivó incluir "editing" y "transcribed_pending": un job
# que entra a edit_pipeline y el worker muere (timeout, OOM, deploy) se
# queda colgado en status="editing" para siempre porque no hay heartbeat
# de auto-fail. Sin esto, el operador no puede limpiar la fila y termina
# pidiendo al admin que actualice la DB a mano.
_DELETABLE_STATUSES = {
    "processing", "queued", "error", "validation_failed",
    "editing",              # edit pipeline corriendo o colgada
    "transcribed_pending",  # tras pérdida de wizard state
}


def delete_job(db: Session, job_id: str, tenant_id: str) -> tuple[bool, str]:
    """Hard-delete a job row owned by `tenant_id`. Returns (ok, reason).

    Safety: only stuck/failed jobs can be deleted — done/pending_review jobs
    must be kept for the audit trail (UMG compliance + plan-quota counting).
    The operator's intent here is cleaning up junk, not erasing approved
    deliveries.

    AIProvenance has a NOT NULL FK to jobs.job_id without ON DELETE CASCADE,
    so we have to clean up its rows manually before the parent delete or
    Postgres raises IntegrityError → 500. Failed/stuck jobs may have started
    accumulating provenance entries (e.g. lyrics_reference_fetch attempts)
    even though the render never completed.
    """
    from database import AIProvenance  # local import to avoid circular

    job = db.query(Job).filter(Job.job_id == job_id, Job.tenant_id == tenant_id).first()
    if not job:
        return False, "not_found"
    if job.status not in _DELETABLE_STATUSES:
        return False, f"protected_status:{job.status}"
    _delete_r2_objects(db, job)
    db.query(AIProvenance).filter(AIProvenance.job_id == job_id).delete(synchronize_session=False)
    db.delete(job)
    db.commit()
    return True, "ok"


def bulk_delete_jobs(db: Session, job_ids: list[str], tenant_id: str) -> dict:
    """Delete many jobs in one transaction. Returns {deleted: [...], skipped: {id: reason}}.

    Skipped reasons: 'not_found', 'protected_status:<status>'. The endpoint
    surfaces this dict so the operator sees exactly which IDs were ignored
    and why.
    """
    from database import AIProvenance

    deleted: list[str] = []
    skipped: dict[str, str] = {}
    if not job_ids:
        return {"deleted": deleted, "skipped": skipped}

    rows = (
        db.query(Job)
        .filter(Job.tenant_id == tenant_id, Job.job_id.in_(job_ids))
        .all()
    )
    found_ids = {r.job_id for r in rows}
    for jid in job_ids:
        if jid not in found_ids:
            skipped[jid] = "not_found"

    deletable_ids: list[str] = []
    for r in rows:
        if r.status not in _DELETABLE_STATUSES:
            skipped[r.job_id] = f"protected_status:{r.status}"
        else:
            deletable_ids.append(r.job_id)

    if deletable_ids:
        # Collect R2 keys from already-fetched rows BEFORE the bulk DELETE
        # removes them — the bulk query returns no data after deletion.
        deletable_set = set(deletable_ids)
        r2_rows = [r for r in rows if r.job_id in deletable_set]

        db.query(AIProvenance).filter(AIProvenance.job_id.in_(deletable_ids)).delete(synchronize_session=False)
        db.query(Job).filter(Job.tenant_id == tenant_id, Job.job_id.in_(deletable_ids)).delete(synchronize_session=False)
        db.commit()
        deleted = deletable_ids

        # Best-effort R2 cleanup after successful DB commit. The bulk
        # DELETE above has already removed every job_id in `r2_rows`
        # from the DB, so the sibling-count inside _delete_r2_objects
        # only sees siblings OUTSIDE this bulk batch. That's the right
        # behavior: if the operator nuked every variant of an audio in
        # one click, the audio gets reclaimed; if any sibling was left
        # behind, it stays protected.
        for r in r2_rows:
            _delete_r2_objects(db, r)

    return {"deleted": deleted, "skipped": skipped}


# bg_preview ghost jobs (Capa C 2026-05-24) tracking-only rows the wizard
# polls while pre-rendering the Veo background during lyrics edit. They
# share the jobs table with real renders for status polling, but the user
# never asked for them as a "video" — they shadow the real job and
# duplicate the song in Historial (e.g. "Hermanos De Sangre" appears with
# status `bg_preview_done` next to the real one in `processing`). Filter
# them at the user-facing read boundary. Admin paths use
# `get_all_jobs_admin` and still see ghosts for debugging.
_BG_PREVIEW_STATUSES = (
    "bg_preview_queued", "bg_preview_generating",
    "bg_preview_done", "bg_preview_failed",
)


def get_all_jobs(
    db: Session,
    tenant_id: str = "default",
    limit: int = 200,
    user_id: int = None,
) -> list[dict]:
    """Return all jobs for a tenant, sorted by creation time (newest first).

    Pass user_id for self-serve callers — see get_job() for rationale.

    Excludes bg_preview ghost rows. Admin callers should use
    `get_all_jobs_admin` if they need them.
    """
    query = db.query(Job).filter(
        Job.tenant_id == tenant_id,
        ~Job.status.in_(_BG_PREVIEW_STATUSES),
    )
    if user_id is not None:
        query = query.filter(Job.user_id == user_id)
    jobs = (
        query.order_by(Job.created_at.desc())
        .limit(limit)
        .all()
    )
    return [j.to_list_dict() for j in jobs]


_TERMINAL_STATUSES = ("done", "error", "rejected", "validation_failed")
# Sub-partition of terminal statuses by outcome. The pipeline writes
# done/pending_review only after deliverables actually land in R2; once
# that ground truth is in the row, NO failure-shaped status ever
# downgrades it. This is the system-wide answer to the test case "callback
# does not clobber terminal states" — any late-firing error path (RQ
# failure callback that races a successful commit, reaper that races a
# completing worker, edit failure that ran after the worker recovered)
# now bounces off this guard inside update_job instead of having to
# re-check the row in every call site.
_SUCCESS_TERMINAL_STATUSES = ("done", "pending_review")
_FAILURE_TARGET_STATUSES = (
    "error", "rejected", "validation_failed", "transcription_failed",
    "bg_preview_failed",
)


def update_job(job_id: str, **kwargs) -> None:
    """Update fields on an existing job. Creates its own DB session for thread safety.

    A status update that targets a non-terminal state is IGNORED for jobs
    already in a terminal state — but the OTHER fields in the same kwargs
    are still applied. Pre-audit 2026-05-26 the whole call was dropped,
    which silently lost data on the race:

      T0  reaper marks status="error" (transcription took >120 min)
      T1  worker actually finishes 30 s later and calls
          update_job(status="transcribed_pending",
                     segments_json=[...],
                     current_step="editing")
      T2  guard sees row.status="error" + target non-terminal → early return
          → segments_json dropped → operator retries → Whisper runs again
          from scratch, $0.006 + 15 min wasted.

    Now we only strip the status key (and the dependent current_step /
    progress when status was the trigger) and keep persisting the rest.

    This guards against:
      - A stale worker thread flushing status="processing" after a reaper
        marked the job error → resurrects a closed job.
      - Two workers picking the same job and both calling
        update_job(status="processing") → double-processing.

    Updates that target a terminal state OR fields that are safe to set on
    terminal jobs (s3_keys, youtube_data, validation_result, etc.) are
    always applied — the reaper itself relies on the terminal-update path.
    """
    from database import SessionLocal

    target_status = kwargs.get("status")
    target_is_terminal = target_status in _TERMINAL_STATUSES

    db = SessionLocal()
    try:
        # CORRECTNESS (audit 2026-05-24): take a row lock so concurrent
        # `update_job` calls serialize. Without `with_for_update()`,
        # two callers can read the same row, modify different fields,
        # and last-writer-wins clobbers the other's changes. Real-world
        # impact: `_step(progress=55)` racing the swap-retry
        # `update_job(artist=X, song_title=Y)` would lose either the
        # progress or the corrected metadata.
        job = (
            db.query(Job)
            .filter(Job.job_id == job_id)
            .with_for_update()
            .first()
        )
        if not job:
            return

        # Terminal-state guard (split form): drop only the status field
        # (and current_step, which is the user-facing description of that
        # status) so the rest of the payload — segments_json, files,
        # render_params, etc. — still lands. See docstring for the lost-
        # transcription incident this fixes.
        if (
            job.status in _TERMINAL_STATUSES
            and not target_is_terminal
            and target_status is not None
        ):
            kwargs.pop("status", None)
            kwargs.pop("current_step", None)
            # Don't touch progress either — it's tied to the status flow.
            kwargs.pop("progress", None)
            if not kwargs:
                return

        # Success-vs-failure guard: never let a failure-shaped status
        # downgrade a row that's already in a success-shaped terminal.
        # Concrete race this protects: RQ pipeline_failure_callback fires
        # 200 ms after the worker commits status="done"; without this
        # guard, the callback's status="error" would clobber the success.
        # The reaper has its own re-fetch-with-lock guard for the same
        # reason; this is the analogous defense for the failure callbacks
        # that route through update_job.
        if (
            target_status in _FAILURE_TARGET_STATUSES
            and job.status in _SUCCESS_TERMINAL_STATUSES
        ):
            kwargs.pop("status", None)
            kwargs.pop("error", None)
            # error_category viaja junto con error — si el error se descarta
            # (el job ya terminó bien), la categoría también.
            kwargs.pop("error_category", None)
            kwargs.pop("current_step", None)
            kwargs.pop("completed_at", None)
            if not kwargs:
                return

        for key, value in kwargs.items():
            if key == "files":
                # Handle the legacy files dict format
                if isinstance(value, dict):
                    if "video_url" in value:
                        job.video_url = value["video_url"]
                    if "short_url" in value:
                        job.short_url = value["short_url"]
                    if "thumbnail_url" in value:
                        job.thumbnail_url = value["thumbnail_url"]
            elif key == "youtube":
                job.youtube_data = value
            elif hasattr(job, key):
                setattr(job, key, value)

        # Heartbeat for the reaper. Any progress update means the worker is
        # alive (even when progress is the same value as before — the call
        # itself proves liveness). reaper.find_stalled_renders flips a
        # processing job to error when this timestamp goes stale, catching
        # workers SIGKILLed during non-AI steps where there is no in-flight
        # AIProvenance row to anchor find_orphan_polling_jobs.
        if "progress" in kwargs:
            job.last_progress_at = datetime.now(timezone.utc)

        # Mark completed_at on any terminal transition (done, pending_review,
        # error, rejected, validation_failed). Used by the frontend to show
        # the "took X minutes" duration; also matters for forensics — without
        # it, an error job has no end timestamp and you can't tell whether
        # it died in 5 s or 45 min. Pre-2026-05-26 only done/pending_review
        # set this; the reaper and pipeline_failure_callback set it inline
        # in their direct UPDATEs, but the migration to update_job needs
        # the helper to do it consistently.
        if (
            kwargs.get("status") in ("done", "pending_review", "error", "rejected", "validation_failed")
            and not job.completed_at
        ):
            job.completed_at = datetime.now(timezone.utc)

        db.commit()
    except Exception:
        # Without an explicit rollback the session is returned to the pool
        # holding an open transaction; pool_pre_ping only catches
        # disconnects, not in-tx errors, so the next caller can hit
        # "current transaction is aborted, commands ignored until end of
        # transaction block".
        db.rollback()
        raise
    finally:
        db.close()


def get_all_jobs_admin(db: Session, limit: int = 500, offset: int = 0) -> list[dict]:
    """Return all jobs across all tenants (admin only)."""
    jobs = (
        db.query(Job)
        .order_by(Job.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return [j.to_dict() for j in jobs]


def heartbeat(job_id: str) -> bool:
    """U10 (audit 2026-05-25): bump Job.last_progress_at = NOW() sin
    cambiar ningún otro campo. Ligero, atomic, idempotent.

    Uso: workers en steps largos sin update_job() natural (Veo polling
    de hasta 10min, retry loops). Le dice al reaper "estoy vivo".

    Best-effort: cualquier excepción se traga (heartbeat no debe romper
    el render).
    """
    from database import SessionLocal
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.job_id == job_id).first()
        if not job:
            return False
        job.last_progress_at = datetime.now(timezone.utc)
        db.commit()
        return True
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return False
    finally:
        db.close()


def merge_render_params(job_id: str, new_params: dict) -> bool:
    """Atomic merge de `new_params` en Job.render_params (dict JSONB).

    Resuelve el race U5 (audit 2026-05-25): 2 edits concurrentes al
    mismo job (operator A toca typography, operator B toca background)
    leen render_params al inicio del endpoint, mutan local, ambos
    commitean. Last-writer-wins → la modificación del primero se pierde.

    El mismo patrón ocurre en pipeline.py:703 (Capa C bg_preview merge)
    si el worker corre concurrente al /edit endpoint.

    Una sola tx con SELECT FOR UPDATE garantiza que el read + merge +
    write sean atómicos respecto a otros callers. Postgres serializa
    los waiters en el lock.

    Args:
        job_id: target job
        new_params: dict con las keys/values a mergear en render_params.
                    Keys con valor None NO se mergean (NO_OP — para que
                    los callers puedan pasar dicts con keys opcionales).

    Returns:
        True si actualizó (o no-op idempotente), False si el job no existe.
    """
    if not new_params:
        return True
    from database import SessionLocal
    db = SessionLocal()
    try:
        job = (
            db.query(Job)
            .filter(Job.job_id == job_id)
            .with_for_update()
            .first()
        )
        if not job:
            return False
        existing = dict(job.render_params or {})
        changed = False
        for k, v in new_params.items():
            if v is None:
                continue
            if existing.get(k) != v:
                existing[k] = v
                changed = True
        if changed:
            job.render_params = existing
            db.commit()
        return True
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return False
    finally:
        db.close()


def merge_s3_keys(job_id: str, file_type: str, key: str) -> bool:
    """Atomic merge de un (file_type → key) en Job.s3_keys.

    Resuelve el race U1 (audit 2026-05-25): dos prewarm workers
    (umg_master + umg_short) ambos snapshotean s3_keys=None ANTES del
    transcode (60-300s), luego compiten al final por escribir su key.
    El read-modify-write fuera de la misma tx + el setattr() de
    update_job pisa el otro. Prod 2026-05-12: 8/18 jobs UMG perdieron
    una key, reconciliados a mano por SQL.

    Una sola tx con SELECT FOR UPDATE: read y write atómicos.
    Returns True si actualizó, False si el job no existe.
    """
    from database import SessionLocal
    db = SessionLocal()
    try:
        job = (
            db.query(Job)
            .filter(Job.job_id == job_id)
            .with_for_update()
            .first()
        )
        if not job:
            return False
        current = dict(job.s3_keys or {})
        if current.get(file_type) == key:
            return True
        current[file_type] = key
        job.s3_keys = current
        db.commit()
        return True
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def remove_s3_keys(job_id: str, file_types: list[str]) -> bool:
    """Atomically drop one or more (file_type → key) entries from Job.s3_keys.

    Counterpart to `merge_s3_keys`. Used by run_edit_pipeline to invalidate
    the lazy ProRes derivatives (umg_master / umg_short) after a re-render:
    the .mov is NOT regenerated by the edit (it's lazy-transcoded from
    lyric_video.mp4 at download time), so a stale s3_keys["umg_master"]
    would keep serving the pre-edit cut on /download/{id}/umg_master while
    the freshly re-rendered MP4 download serves the new cut. The prior key
    is preserved as {key}.vN by _snapshot_previous_deliverables, so dropping
    it here does not lose the rollback path.

    SELECT FOR UPDATE so a concurrent merge_s3_keys (e.g. a prewarm worker)
    can't race-restore the key between our read and write.

    Returns True if the row exists (whether or not any key was present),
    False if the job is gone.
    """
    from database import SessionLocal
    db = SessionLocal()
    try:
        job = (
            db.query(Job)
            .filter(Job.job_id == job_id)
            .with_for_update()
            .first()
        )
        if not job:
            return False
        current = dict(job.s3_keys or {})
        changed = False
        for ft in file_types:
            if ft in current:
                del current[ft]
                changed = True
        if changed:
            job.s3_keys = current
            db.commit()
        return True
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_jobs_stats(db: Session, tenant_id: str = None) -> dict:
    """Get aggregate stats. If tenant_id is None, returns global stats."""
    query = db.query(Job)
    if tenant_id:
        query = query.filter(Job.tenant_id == tenant_id)

    total = query.count()
    done = query.filter(Job.status == "done").count()
    errors = query.filter(Job.status == "error").count()
    processing = query.filter(Job.status == "processing").count()

    return {
        "total": total,
        "done": done,
        "errors": errors,
        "processing": processing,
    }
