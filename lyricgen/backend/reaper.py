"""Stuck-job reaper.

Detects jobs that have been in `processing` / `queued` longer than a
configured threshold and marks them as `error` so the operator's UI
shows them as failed instead of a forever-spinning "Generando".

Why we need it:
  - RQ workers can die mid-render (deploy, OOM, signal). RQ moves the
    RQ-side job to FailedJobRegistry with AbandonedJobError, but the
    Postgres `jobs` row stays at status="processing" because the worker
    never reaches its except handler.
  - Without a reaper, the operator sees a zombie that they have to
    manually delete from /admin every time. This module turns it into
    a no-touch failure path.

Side effects per reaped job:
  - Postgres: status flips to "error" with a clear message.
  - AuditLog row recorded (action="reaper.killed").
  - Sentry: capture_message at ERROR level, tagged with job_id and
    tenant_id.
  - Email: a single batched message to OWNER_EMAIL summarising every
    job killed in this pass (so a 30-zombie storm is one notification,
    not 30).

Threshold is read from env REAPER_THRESHOLD_MIN (default 180). The
default (U10 audit 2026-05-25) is 3h to cover worst-case UMG ProRes
renders (30-90min) with safe margin against premature reaping.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import exists, func
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from database import AIProvenance, AuditLog, Job, SessionLocal

# Markers for Postgres connection drops that we should silently retry
# instead of letting the reaper thread emit Sentry noise every 5 min.
# Same list as the HTTP retry middleware in main.py — kept duplicated
# here because the reaper runs in a thread, bypassing the ASGI stack.
_TRANSIENT_DB_MARKERS = (
    "SSL connection has been closed",
    "server closed the connection",
    "connection already closed",
    "could not connect to server",
)

logger = logging.getLogger("genly.reaper")

# Reaped jobs land at this status (the operator-facing status). Not
# "abandoned" because the existing UI / batches don't know that one.
_REAPED_STATUS = "error"

# How old a job needs to be before we consider it dead.
# U10 (audit 2026-05-25): bumped 100→180 (3h). UMG ProRes en pico real
# tarda 30-90min; margen de 10min era thin. 3h cubre el peor caso
# (4K@60 con retries Veo) sin abrir ventana de reap-too-late razonable.
# El worker emite heartbeat explícito en Veo polling (pipeline.py
# heartbeat()) así el threshold solo importa para SIGKILL real.
_DEFAULT_THRESHOLD_MIN = int(os.environ.get("REAPER_THRESHOLD_MIN", "180"))

# Fast-lane threshold for jobs whose latest ai_provenance row is still
# in-flight (duration_ms NULL). Veo polling, Whisper, Gemini etc. all
# record a provenance row at call start and update duration_ms when the
# call returns. If that row is older than this, the worker died mid-
# call (deploy/OOM/crash) and the DB row is a zombie. 10 min is safely
# longer than any healthy single AI call (Veo: ~2 min p99, Whisper:
# ~3 min for 7-min audio) but short enough that the user sees the
# failure surface inside one coffee break instead of two hours later.
_ORPHAN_POLL_THRESHOLD_MIN = int(os.environ.get(
    "REAPER_ORPHAN_POLL_THRESHOLD_MIN", "10",
))

# transcribed_pending jobs are uploaded but waiting on the user to finish
# the lyrics editor and click Generate. They consume disk + R2 storage,
# and abandoned ones (closed tab, lost connection) accumulate forever
# without a separate sweep. A 30-min TTL covers the realistic editing
# window without prematurely killing live sessions.
_TRANSCRIBED_PENDING_TTL_MIN = int(os.environ.get(
    "REAPER_TRANSCRIBED_PENDING_TTL_MIN", "30",
))

# awaiting_upload jobs are even shorter-lived: the browser is mid-PUT to
# R2 with a presigned URL. If the user closes the tab partway through, we
# need to abort the multipart upload so R2 doesn't keep the parts around
# (R2 charges for the storage either way until the abort fires).
_AWAITING_UPLOAD_TTL_MIN = int(os.environ.get(
    "REAPER_AWAITING_UPLOAD_TTL_MIN", "20",
))

# Edit-request abandon threshold. The worst case is a background edit
# which re-runs Veo (~3 min p99) plus the full video composite (~5-8 min
# for a 4-min song). 30 min gives 2-3× headroom over the slowest healthy
# edit; anything older than that is a worker death (deploy/OOM) we need
# to surface to the user instead of leaving them watching "40%" forever.
_EDIT_ABANDON_THRESHOLD_MIN = int(os.environ.get(
    "REAPER_EDIT_ABANDON_THRESHOLD_MIN", "30",
))

# Stuck-transcription threshold. RQ kills the transcription_worker at
# TRANSCRIBE_JOB_TIMEOUT=1800s (30 min) per attempt; Retry(max=2, interval=10)
# allows 2 additional attempts, so a healthy lifetime in transcribing
# states maxes out around ~90 min worst case. 120 min gives ~33% margin.
#
# Why a dedicated sweep instead of folding into find_stuck_jobs:
#   - find_stuck_jobs uses _DEFAULT_THRESHOLD_MIN=180 (3h) to accommodate
#     UMG ProRes renders; that ceiling is way too generous for Whisper
#     (worst case ~30 min per attempt). A transcription stuck 2h is
#     definitely a zombie, not slow.
#   - find_stuck_jobs reaps to status="error" (the generic render-failed
#     state). The transcription pipeline has its own terminal status
#     `transcription_failed` which the editor uses to show "Reintentar"
#     with the right CTA — different button than the post-render retry.
#
# Why this matters operationally:
#   - The transcription pipeline registers on_failure=transcription_failure_callback
#     (queue_jobs.py:465) which is supposed to flip the row to
#     transcription_failed when retries are exhausted. But the callback
#     never fires when RQ Retry schedules a deferred retry that the
#     scheduler never executes (worker.py had `with_scheduler=False` until
#     2026-05-26 — fixed in the same PR introducing this sweep). The row
#     stays in `transcribing` / `transcribing_queued` indefinitely.
#   - Even with the scheduler fix, this sweep is the belt-and-suspenders
#     for any future regression that strands a transcription (Whisper
#     hangs without crashing, DB write fails after status flip, etc.).
_TRANSCRIPTION_STUCK_THRESHOLD_MIN = int(os.environ.get(
    "REAPER_TRANSCRIPTION_STUCK_THRESHOLD_MIN", "120",
))

# Stalled-render threshold. Catches jobs in `processing` whose progress
# hasn't moved in N minutes — the gap between find_orphan_polling_jobs
# (which fires on stale in-flight AI calls) and find_stuck_jobs (which
# uses a 100-min created_at threshold). A healthy worker calls
# jobs.update_job(progress=...) at every step transition and at multiple
# checkpoints within a step, so a 20-min gap is a strong "worker is dead"
# signal. Veo polling can pause progress ~3 min p99, ffmpeg composite can
# run silently ~5 min on long songs — 20 min gives 4-6× headroom over any
# healthy phase.
_STALLED_RENDER_THRESHOLD_MIN = int(os.environ.get(
    "REAPER_STALLED_RENDER_THRESHOLD_MIN", "20",
))

# Ops inbox for the reaper digest. This is an INTERNAL operations alert
# (technical: "OOM, hung ffmpeg, Veo 429-storm") — it must NOT default to
# a real person's customer-facing address. Until 2026-05-20 it defaulted
# to tomas@epical.digital, which doubles as a test USER account, so the
# technical digest kept landing in a customer-shaped inbox.
#
# Resolution order:
#   1. OWNER_EMAIL  — explicit override for this digest specifically
#   2. ALERT_EMAIL  — the existing ops-alert convention (preflight smoke
#                     tests use the same var; see scripts/preflight/)
#   3. ops@genly.pro — internal default on our own domain, no PII
#
# Set OWNER_EMAIL (or ALERT_EMAIL) in Railway to wherever ops actually
# reads alerts. If ops@genly.pro has no mailbox, add a forwarding rule
# in the genly.pro domain so the digest reaches someone.
_OWNER_EMAIL = (
    os.environ.get("OWNER_EMAIL")
    or os.environ.get("ALERT_EMAIL")
    or "ops@genly.pro"
)


def find_stuck_jobs(db: Session, threshold_min: int = _DEFAULT_THRESHOLD_MIN) -> list[Job]:
    """Return jobs in processing/queued whose staleness anchor is older
    than threshold. Pending_review is intentionally excluded — those are
    waiting on a human, not a worker.

    Staleness anchor is coalesce(last_progress_at, created_at):
      - Worker calls update_job(progress=X) at every step; last_progress_at
        ticks on each call. A genuine worker death stops the ticks → the
        coalesce falls forward to a stale value and the row gets reaped.
      - A queued row that no worker has touched yet has last_progress_at
        NULL → coalesce falls back to created_at, preserving the original
        "100 min in the queue is dead" guarantee.
      - /retry resets last_progress_at = NOW(), so a retried job created
        12 h ago is no longer insta-killed on the next reaper sweep.
        Pre-fix incident 2026-05-15: programmatic retry of 4 omg jobs
        from 13:45 was killed at 16:51 because the reaper still anchored
        on created_at."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=threshold_min)
    anchor = func.coalesce(Job.last_progress_at, Job.created_at)
    return (
        db.query(Job)
        # U7 (audit 2026-05-25): bg_preview_queued agregado al filter.
        # Capa C 2026-05-24 introdujo este estado para "ghost jobs" de
        # pre-generación del background. Si el operador cierra el browser
        # sin completar el wizard, el job queda colgado forever (R2
        # cache_key persistente). Threshold de 100min es generoso para
        # un preview que tarda 30-60s normalmente.
        .filter(Job.status.in_(["processing", "queued", "bg_preview_queued"]))
        .filter(anchor < cutoff)
        .order_by(anchor.asc())
        .all()
    )


def find_orphan_polling_jobs(
    db: Session,
    threshold_min: int = _ORPHAN_POLL_THRESHOLD_MIN,
) -> list[Job]:
    """Return jobs whose latest ai_provenance row is an in-flight call
    (duration_ms NULL) older than threshold. This is the deploy-death
    signal: provenance.record_ai_call() inserts the row at call start
    and only fills duration_ms when the call returns. A stale NULL row
    means the worker died mid-poll (Railway redeploy, OOM, hard signal)
    and the job is a zombie — current_step/progress will sit forever.

    Faster path than find_stuck_jobs (which uses created_at and a
    conservative 100 min threshold) because we have a precise signal:
    we KNOW the AI call started and never finished, instead of guessing
    from age."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=threshold_min)
    orphan_provenance = (
        exists()
        .where(AIProvenance.job_id == Job.job_id)
        .where(AIProvenance.duration_ms.is_(None))
        .where(AIProvenance.created_at < cutoff)
    )
    return (
        db.query(Job)
        .filter(Job.status == "processing")
        .filter(orphan_provenance)
        .order_by(Job.created_at.asc())
        .all()
    )


def find_abandoned_transcribed(
    db: Session,
    ttl_min: int = _TRANSCRIBED_PENDING_TTL_MIN,
) -> list[Job]:
    """Return jobs stuck in transcribed_pending past the editing TTL.
    These represent users who transcribed but never clicked Generate
    (closed tab, lost connection). The associated audio file lives on
    disk + R2 and needs to be reaped or it accumulates forever.

    Staleness anchor is coalesce(last_user_activity_at, created_at): any
    authenticated touch (POST /save-segments, /status poll, etc) bumps
    last_user_activity_at, so an active batch-edit session keeps the job
    alive past the TTL. Older rows with NULL last_user_activity_at fall
    back to created_at — preserves pre-migration behavior.

    Incident 2026-05-14: a user batch-editing 5 lyrics for 90 min got
    reaped at 30 min and lost everything because the anchor was just
    created_at. The coalesce + /save-segments together fix that.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=ttl_min)
    anchor = func.coalesce(Job.last_user_activity_at, Job.created_at)
    return (
        db.query(Job)
        .filter(Job.status == "transcribed_pending")
        .filter(anchor < cutoff)
        .order_by(Job.created_at.asc())
        .all()
    )


def find_abandoned_uploads(
    db: Session,
    ttl_min: int = _AWAITING_UPLOAD_TTL_MIN,
) -> list[Job]:
    """Return jobs stuck in awaiting_upload past the upload TTL. The
    browser is supposed to PUT bytes directly to R2 within minutes of
    /upload-url; anything older than ttl_min is a closed tab / abandoned
    multipart upload."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=ttl_min)
    return (
        db.query(Job)
        .filter(Job.status == "awaiting_upload")
        .filter(Job.created_at < cutoff)
        .order_by(Job.created_at.asc())
        .all()
    )


def find_stalled_renders(
    db: Session,
    threshold_min: int = _STALLED_RENDER_THRESHOLD_MIN,
) -> list[Job]:
    """Return jobs in `processing` whose progress hasn't moved in
    threshold_min minutes.

    The scenario this catches: worker SIGKILLed during ffmpeg / moviepy /
    R2 upload — a non-AI step where there is no in-flight AIProvenance
    row to anchor find_orphan_polling_jobs, and find_stuck_jobs's 100-min
    created_at cutoff is too long. Confirmed in prod 2026-05-12: job
    2144aacb453e killed at video/40% during a deploy, invisible to any
    reaper for 87 min.

    Signal: last_progress_at, written by jobs.update_job() at every
    progress call. A healthy worker hits multiple progress checkpoints
    per minute across whisper / background / video / thumbnail steps.
    A 20-min gap means the worker is gone.

    Rows with last_progress_at IS NULL are skipped — they predate this
    column (or status=queued, never picked up). The age-based
    find_stuck_jobs covers those at 100 min.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=threshold_min)
    return (
        db.query(Job)
        .filter(Job.status == "processing")
        .filter(Job.last_progress_at.isnot(None))
        .filter(Job.last_progress_at < cutoff)
        .order_by(Job.last_progress_at.asc())
        .all()
    )


def find_abandoned_edits(
    db: Session,
    threshold_min: int = _EDIT_ABANDON_THRESHOLD_MIN,
) -> list[Job]:
    """Return jobs stuck in `status='editing'` past the edit threshold.

    The scenario this catches: operator clicks "Regenerate background" /
    "Change typography" / "Fix lyrics", worker picks up the RQ job, then
    Railway redeploys mid-render. Worker process dies. RQ moves the
    queue entry to FailedJobRegistry with AbandonedJobError, but the
    Postgres row stays at status='editing'/progress=N% because the worker
    never reached its except handler. From the user's POV, the video is
    stuck at "40%" indefinitely.

    Why we don't use created_at like find_stuck_jobs: lyrics edits are
    allowed from `done` and `rejected` status, so editing_started_at
    might be hours/days after created_at. The dedicated timestamp is set
    by the /edit handler (main.py) the moment the row flips to editing.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=threshold_min)
    return (
        db.query(Job)
        .filter(Job.status == "editing")
        .filter(Job.editing_started_at.isnot(None))
        .filter(Job.editing_started_at < cutoff)
        .order_by(Job.editing_started_at.asc())
        .all()
    )


def find_stuck_transcriptions(
    db: Session,
    threshold_min: int = _TRANSCRIPTION_STUCK_THRESHOLD_MIN,
) -> list[Job]:
    """Return jobs in `transcribing_queued` / `transcribing` older than
    threshold. These are NOT covered by find_stuck_jobs (which only sweeps
    processing/queued/bg_preview_queued) — see threshold docstring for the
    full rationale.

    Staleness anchor is coalesce(last_progress_at, created_at): worker calls
    update_job(status="transcribing", current_step="transcribing") at the
    start of `run_transcription_job`, which ticks last_progress_at via the
    update_job pipeline. A healthy in-flight transcription bumps it; a
    worker that never picked up the job (or died before the status flip)
    has NULL → falls back to created_at."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=threshold_min)
    anchor = func.coalesce(Job.last_progress_at, Job.created_at)
    return (
        db.query(Job)
        .filter(Job.status.in_(["transcribing_queued", "transcribing"]))
        .filter(anchor < cutoff)
        .order_by(anchor.asc())
        .all()
    )


def _reason_for_transcription(job: Job) -> str:
    """Customer-facing message for a reaped stuck transcription. The
    `transcription_failed` UI surfaces a "Reintentar" CTA that re-runs the
    transcribe step against the audio still sitting on R2 — no re-upload
    needed. Same tone as _reason_for_orphan."""
    return (
        "La transcripción se interrumpió por un problema temporal del "
        "servidor. Tu archivo sigue guardado: apretá \"Reintentar\" para "
        "volver a transcribir."
    )


def reap_stuck_transcription(db: Session, job: Job) -> None:
    """Flip a stuck transcription to `transcription_failed` and cancel its
    RQ entry. Caller commits.

    Different reaping path from `reap_stuck_job` because:
      1. Status target is `transcription_failed` (editor's "Reintentar" CTA),
         not the generic `error` (post-render retry).
      2. Transcription RQ ids are prefixed `transcribe:<job_id>` to avoid
         collision with the render job sharing the same Postgres job_id;
         cancel_rq_job is called with the prefixed form.
    """
    # Race guard (audit 2026-05-26): the row read in find_stuck_transcriptions
    # had no lock; the worker may have finished between then and now.
    # Re-fetch with FOR UPDATE and re-check. Without this, the reaper
    # would clobber a successful transcribed_pending with transcription_failed
    # and the operator would lose segments_json they could have edited.
    locked = (
        db.query(Job)
        .filter(Job.job_id == job.job_id)
        .with_for_update()
        .first()
    )
    if locked is None:
        return
    if locked.status not in ("transcribing_queued", "transcribing"):
        logger.info(
            "reaper: skip transcription %s — status changed under us (was %s, now %s)",
            job.job_id, job.status, locked.status,
        )
        return

    rq_removed = False
    previous_status = locked.status  # capture before mutation for audit
    try:
        from queue_jobs import cancel_rq_job
        # enqueue_transcription uses `transcribe:<job_id>` as the RQ id
        # (queue_jobs.py:457). Cancelling the bare job_id would miss it.
        rq_removed = cancel_rq_job(f"transcribe:{locked.job_id}")
    except Exception as e:  # pragma: no cover
        logger.warning(
            "cancel_rq_job (transcription) failed for %s: %s", locked.job_id, e,
        )
    reason = _reason_for_transcription(locked)
    locked.status = "transcription_failed"
    locked.current_step = "error"
    locked.error = reason
    locked.completed_at = datetime.now(timezone.utc)
    db.add(AuditLog(
        action="reaper.killed_transcription",
        detail={
            "job_id": locked.job_id,
            "tenant_id": locked.tenant_id,
            "artist": locked.artist,
            "filename": locked.filename,
            "previous_status": previous_status,
            "age_minutes": round(_age_minutes(locked), 1),
            "reason": reason,
            "rq_removed": rq_removed,
        },
    ))


def revert_abandoned_edit(db: Session, job: Job) -> None:
    """Roll an abandoned edit back to pending_review so the user can
    re-try. The video on R2 from the prior successful render is still
    intact (the worker overwrites only at the very end of the pipeline,
    after the composite is finalized — if it died mid-render no bytes
    were written). Decrement edit_count so the failed attempt doesn't
    burn one of the operator's 3 allowed edits — Railway's fault, not
    theirs. Caller commits.

    Also cancels the RQ entry so a worker restart can't resurrect the
    half-finished edit and silently overwrite the user's good video.
    """
    # Race guard (audit 2026-05-26): re-fetch with lock and confirm the
    # row is still in `editing` before reverting. The worker may have
    # finished the edit successfully between the sweep and now; reverting
    # those rows would dump the operator back to pending_review on a video
    # that actually got the edit applied.
    locked = (
        db.query(Job)
        .filter(Job.job_id == job.job_id)
        .with_for_update()
        .first()
    )
    if locked is None:
        return
    if locked.status != "editing":
        logger.info(
            "reaper: skip edit-revert %s — status changed under us (was %s, now %s)",
            job.job_id, job.status, locked.status,
        )
        return

    try:
        from queue_jobs import cancel_rq_job
        # enqueue_edit uses RQ job id `edit:<job_id>` (queue_jobs.py:694)
        # to avoid collision with the render job sharing the same Postgres
        # job_id. Cancelling the bare job_id would miss the RQ entry, the
        # ScheduledJobRegistry retry timer would fire, and a fresh worker
        # would run the half-finished edit against this row already flipped
        # back to pending_review — exactly the regression the docstring
        # above promises to prevent. Audit 2026-05-26.
        cancel_rq_job(f"edit:{locked.job_id}")
    except Exception as e:  # pragma: no cover
        logger.warning("cancel_rq_job (edit) failed for %s: %s", locked.job_id, e)
    prev_edit_count = locked.edit_count or 0
    new_edit_count = max(0, prev_edit_count - 1)
    # Capture step/progress at-revert-time so the AuditLog "previous" block
    # shows where the edit actually died (e.g. step="video", progress=40).
    # Pre-2026-05-26 the audit read these AFTER the mutation below and
    # always logged "thumbnail"/100 — useless for forensics.
    prev_current_step = locked.current_step
    prev_progress = locked.progress
    age = 0.0
    if locked.editing_started_at:
        started = locked.editing_started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - started).total_seconds() / 60.0
    locked.status = "pending_review"
    # The pre-edit terminal state was current_step="thumbnail"/progress=100
    # for any job that made it to pending_review. Restoring those values
    # keeps the progress bar / status badge consistent with what the user
    # saw before they clicked the edit button.
    locked.current_step = "thumbnail"
    locked.progress = 100
    locked.edit_count = new_edit_count
    locked.editing_started_at = None
    locked.error = None
    db.add(AuditLog(
        action="reaper.reverted_edit",
        detail={
            "job_id": locked.job_id,
            "tenant_id": locked.tenant_id,
            "artist": locked.artist,
            "song_title": locked.song_title,
            "age_minutes": round(age, 1),
            "reason": (
                "Edit request abandoned by worker (probable Railway deploy "
                "or OOM mid-render). Reverted to pending_review with "
                "edit_count restored so the user can re-try at no cost."
            ),
            "previous": {
                "status": "editing",
                "current_step": prev_current_step,
                "progress": prev_progress,
                "edit_count": prev_edit_count,
            },
            "now": {
                "status": "pending_review",
                "edit_count": new_edit_count,
            },
        },
    ))


def _age_minutes(job: Job) -> float:
    if not job.created_at:
        return 0.0
    created = job.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created).total_seconds() / 60.0


def _reason_for(job: Job) -> str:
    # Customer-facing — shown verbatim in the UI error banner. Keep it
    # friendly and actionable; NO internal jargon ("Worker", "container
    # restart", "timeout o crash"). The age-based sweep is the catch-all
    # detector so the true cause is genuinely unknown — "problema
    # temporal del servidor" is the honest customer summary. The CTA
    # matches the button actually rendered ("Reintentar sin re-subir"),
    # which has its own R2 pre-check that surfaces a re-upload prompt
    # only if the audio is genuinely gone. Sibling messages
    # (_reason_for_orphan, _reason_for_stalled) use the same tone.
    return (
        "El video se interrumpió por un problema temporal del servidor "
        "y no llegó a completarse. Tu archivo sigue guardado: apretá "
        "\"Reintentar sin re-subir\" para volver a procesarlo."
    )


def _reason_for_orphan(job: Job) -> str:
    return (
        "El servidor se reinició mientras generábamos el video de fondo. "
        "Tu MP3 sigue guardado: apretá \"Reintentar sin re-subir\" "
        "para volver a generarlo."
    )


def _reason_for_stalled(job: Job) -> str:
    return (
        "El servidor se reinició mientras renderizábamos tu video. "
        "Tu archivo sigue guardado: apretá \"Reintentar\" para "
        "volver a procesarlo."
    )


def _delete_abandoned_transcribed(db: Session, job: Job) -> None:
    """Hard-delete an abandoned transcribed_pending row + its audio file.
    The user never finalized the upload, so there's no operator-facing
    artifact to preserve."""
    job_id = job.job_id
    # Local file under OUTPUTS_DIR.
    try:
        from pipeline import OUTPUTS_DIR
        local_dir = os.path.join(OUTPUTS_DIR, job_id)
        if os.path.isdir(local_dir):
            import shutil as _sh
            _sh.rmtree(local_dir, ignore_errors=True)
    except Exception as e:  # pragma: no cover
        logger.debug(f"reaper: local dir cleanup failed for {job_id}: {e}")
    # Cancel any stale RQ entry (best-effort, consistent with the other
    # cleanup paths in this file). enqueue_transcription uses RQ job id
    # `transcribe:<job_id>` (queue_jobs.py:474); the bare job_id form
    # would miss it. Audit 2026-05-26.
    try:
        from queue_jobs import cancel_rq_job
        cancel_rq_job(f"transcribe:{job_id}")
    except Exception as e:  # pragma: no cover
        logger.debug("reaper: cancel_rq_job failed for %s: %s", job_id, e)
    # R2 object — only delete when no sibling job references the same
    # input audio. Variants and edit-side jobs share input_r2_key with
    # the parent; if the reaper kills this row while a sibling is alive,
    # the surviving sibling's next /retry or /edit 404s on R2. Same
    # guard as jobs._delete_r2_objects (incident 2026-05-19, Amanda
    # Pujó "Ser Anti"). Best-effort — failure leaves orphan for the
    # next cleanup_old_inputs sweep.
    if job.input_r2_key:
        try:
            from jobs import input_audio_is_shared
            if not input_audio_is_shared(db, job):
                import storage as _storage
                _storage.delete_object(job.input_r2_key)
            else:
                logger.info(
                    "reaper: keeping R2 input for %s — sibling job(s) still reference %s",
                    job_id, job.input_r2_key,
                )
        except Exception as e:  # pragma: no cover
            logger.debug("reaper: R2 delete failed for %s: %s", job_id, e)
    # AIProvenance has a NOT NULL FK to jobs.job_id without ON DELETE CASCADE,
    # so its rows MUST be cleared before the parent delete — otherwise Postgres
    # raises IntegrityError and the ENTIRE reap transaction aborts, leaving this
    # job (and every later job in the same sweep) un-reaped. Prod 2026-06-02:
    # the reaper crashed on exactly this every cycle (`null value in column
    # "job_id" of relation "ai_provenance"`). Mirrors jobs.delete_job (jobs.py).
    db.query(AIProvenance).filter(AIProvenance.job_id == job_id).delete(synchronize_session=False)
    db.delete(job)


def _delete_abandoned_upload(db: Session, job: Job) -> None:
    """Hard-delete an abandoned awaiting_upload row.

    Three cleanup paths depending on what state the upload reached:
      1. Multipart in-flight → abort_multipart_upload to release the
         parts R2 has accepted so far.
      2. Single-PUT or completed multipart → delete the object.
      3. No R2 key recorded yet (browser never started) → just drop
         the row.
    """
    job_id = job.job_id
    try:
        import storage as _storage
        if job.multipart_upload_id and job.input_r2_key:
            # Multipart abort releases the in-flight parts only. There's
            # no shared-key concern here — multipart uploads are per-job
            # by design (the upload_id is unique to this row).
            _storage.multipart_abort(job.input_r2_key, job.multipart_upload_id)
        elif job.input_r2_key:
            # Same shared-key guard as _reap_stuck_job. An
            # awaiting_upload row with a shared input_r2_key shouldn't
            # exist by design (variants don't go through awaiting_upload
            # state) but the guard is cheap and protects against any
            # future code path that might fork before upload finishes.
            try:
                from jobs import input_audio_is_shared
                if not input_audio_is_shared(db, job):
                    _storage.delete_object(job.input_r2_key)
                else:
                    logger.info(
                        "reaper: keeping abandoned-upload input — sibling refs %s",
                        job.input_r2_key,
                    )
            except Exception:
                pass
    except Exception as e:  # pragma: no cover
        logger.debug(f"reaper: R2 cleanup failed for {job_id}: {e}")
    # Local dir (rare for awaiting_upload, but defensive).
    try:
        from pipeline import OUTPUTS_DIR
        local_dir = os.path.join(OUTPUTS_DIR, job_id)
        if os.path.isdir(local_dir):
            import shutil as _sh
            _sh.rmtree(local_dir, ignore_errors=True)
    except Exception:
        pass
    # Clear provenance rows before the parent delete (NOT NULL FK, no ON DELETE
    # CASCADE) so the reap can't IntegrityError. awaiting_upload rows rarely
    # have provenance, but the guard keeps every reaper delete path consistent
    # with jobs.delete_job and the transcribed-reap path above.
    db.query(AIProvenance).filter(AIProvenance.job_id == job_id).delete(synchronize_session=False)
    db.delete(job)


_REAPABLE_STATUSES = ("processing", "queued", "bg_preview_queued")


def reap_stuck_job(db: Session, job: Job, reason: str) -> bool:
    """Flip the row to error, cancel the RQ job, and log it. Caller commits.

    Returns True if the job was reaped, False if a race made the reap
    unnecessary (worker completed between the find_* SELECT and now).

    RACE GUARD (audit 2026-05-26 systemic-jobs-pipeline): the `job` passed
    in came from a SELECT WITHOUT lock several seconds ago. Between then
    and now, the worker may have finished successfully and flipped the
    row to "done" / "pending_review". Without this re-fetch+recheck, the
    next two lines (`job.status = "error"` + commit) would CLOBBER the
    successful completion — the video sits intact in R2 but the operator
    sees "El video se interrumpió por un problema temporal del servidor"
    on a job that actually worked.

    Pattern: re-read with FOR UPDATE so concurrent update_job calls
    serialize behind us, then verify the row is still in a status we
    consider reapable.

    Cancelling the RQ entry is critical: without it, RQ's Retry / cleanup
    path resurrects the abandoned job on the next worker boot. The worker
    then re-runs the pipeline against a row already marked `error`, and
    `jobs.update_job`'s terminal-state guard silently discards the result
    at the end — 20 min of compute thrown away while the user keeps seeing
    the "Worker abandonó el job" message.

    Incident 2026-05-15: 4 omg jobs got reaped, then resurrected by RQ on
    the next worker restart, re-processed silently, and ended in the same
    `error` state. Fixing the desync here closes the loop.
    """
    # Re-fetch with row lock to avoid the lost-completion race described
    # above. If the row was deleted (operator force-deleted) between the
    # sweep and now, .first() returns None — nothing to reap.
    locked = (
        db.query(Job)
        .filter(Job.job_id == job.job_id)
        .with_for_update()
        .first()
    )
    if locked is None:
        return False
    if locked.status not in _REAPABLE_STATUSES:
        logger.info(
            "reaper: skip %s — status changed under us (was %s, now %s)",
            job.job_id, job.status, locked.status,
        )
        return False

    rq_removed = False
    # find_stuck_jobs sweeps "processing", "queued", AND "bg_preview_queued"
    # (reaper.py:199). The first two map to RQ jobs whose id == job_id (no
    # prefix — see enqueue_pipeline). bg_preview uses `bgpreview:<job_id>`
    # (queue_jobs.py:544); cancelling the bare form would miss it and let
    # the ScheduledJobRegistry resurrect the ghost. Audit 2026-05-26.
    rq_ids_to_try = [locked.job_id]
    if locked.status == "bg_preview_queued":
        rq_ids_to_try = [f"bgpreview:{locked.job_id}"]
    try:
        from queue_jobs import cancel_rq_job
        for rq_id in rq_ids_to_try:
            if cancel_rq_job(rq_id):
                rq_removed = True
                break
    except Exception as e:  # pragma: no cover
        logger.warning("cancel_rq_job failed for %s: %s", locked.job_id, e)
    previous_status = locked.status
    locked.status = _REAPED_STATUS
    locked.error = reason
    locked.completed_at = datetime.now(timezone.utc)
    db.add(AuditLog(
        action="reaper.killed",
        detail={
            "job_id": locked.job_id,
            "tenant_id": locked.tenant_id,
            "artist": locked.artist,
            "filename": locked.filename,
            # Capture the ACTUAL previous status, not the hard-coded
            # "processing" the old code wrote. bg_preview_queued and
            # queued get reaped by this same path; forensics needs the
            # real source state to debug "why was this in X?" later.
            "previous_status": previous_status,
            "current_step": locked.current_step,
            "progress": locked.progress,
            "age_minutes": round(_age_minutes(locked), 1),
            "reason": reason,
            "rq_removed": rq_removed,
        },
    ))
    return True


def _sentry_capture(reaped: list[Job]) -> None:
    """Best-effort Sentry alert. Never raises — failure is logged but
    swallowed so the reaper itself doesn't get killed by observability."""
    if not reaped:
        return
    try:
        import sentry_sdk
        with sentry_sdk.push_scope() as scope:
            scope.set_tag("event", "reaper.killed")
            scope.set_tag("job_count", str(len(reaped)))
            scope.set_extra("jobs", [
                {
                    "job_id": j.job_id,
                    "tenant_id": j.tenant_id,
                    "artist": j.artist,
                    "current_step": j.current_step,
                    "age_min": round(_age_minutes(j), 1),
                }
                for j in reaped
            ])
            tenants = sorted({j.tenant_id for j in reaped if j.tenant_id})
            tenants_str = ", ".join(tenants[:5]) or "—"
            sentry_sdk.capture_message(
                f"Reaper killed {len(reaped)} stuck job(s) ({tenants_str})",
                level="error",
            )
    except Exception as e:  # pragma: no cover
        logger.warning(f"sentry capture failed: {e}")


def _email_owner(reaped: list[Job]) -> None:
    """Single digest email to OWNER_EMAIL summarising the pass. Silently
    no-op if SMTP is not configured (`emails._send_email` checks)."""
    if not reaped:
        return
    try:
        from emails import _send_email, _wrap_template
    except Exception as e:  # pragma: no cover
        logger.warning(f"email module unavailable: {e}")
        return
    try:
        rows = "\n".join(
            f"<tr>"
            f"<td style='padding:6px 12px;font-family:monospace;font-size:11px'>{j.job_id[:12]}…</td>"
            f"<td style='padding:6px 12px'>{j.tenant_id or '—'}</td>"
            f"<td style='padding:6px 12px'>{j.artist or '—'}</td>"
            f"<td style='padding:6px 12px'>{j.current_step or '—'}</td>"
            f"<td style='padding:6px 12px;text-align:right'>{_age_minutes(j):.0f} min</td>"
            f"</tr>"
            for j in reaped
        )
        body = _wrap_template(f"""
            <h2 style="margin:0 0 12px">Reaper killed {len(reaped)} stuck job(s)</h2>
            <p style="color:#555">
              These jobs were in <code>processing</code> for more than
              {_DEFAULT_THRESHOLD_MIN} min and got auto-marked as error.
              Investigate worker logs around the timestamps below to find
              the root cause (container restart, OOM, hung ffmpeg, Veo
              429-storm, etc.).
            </p>
            <table style="border-collapse:collapse;font-size:13px;margin-top:16px">
              <thead>
                <tr style="background:#f6f6f6;text-align:left">
                  <th style="padding:6px 12px">Job</th>
                  <th style="padding:6px 12px">Tenant</th>
                  <th style="padding:6px 12px">Artist</th>
                  <th style="padding:6px 12px">Last step</th>
                  <th style="padding:6px 12px;text-align:right">Age</th>
                </tr>
              </thead>
              <tbody>{rows}</tbody>
            </table>
        """)
        _send_email(
            _OWNER_EMAIL,
            f"[GenLy OPS · interno] {len(reaped)} stuck job(s) reaped",
            body,
        )
    except Exception as e:  # pragma: no cover
        logger.warning(f"reaper email failed: {e}")


# Postgres advisory lock key. 64-bit signed int, must be the same
# across every replica that wants to coordinate. The constant is
# arbitrary; just don't reuse it elsewhere in the app.
_REAPER_ADVISORY_LOCK_KEY = 9118364455199101


def reap_all_stuck(threshold_min: int = _DEFAULT_THRESHOLD_MIN) -> int:
    """Public entrypoint with a transient-error retry. The reaper runs
    in a background thread (main.py:_reaper_loop) which is OUTSIDE the
    ASGI stack — the HTTP retry middleware can't catch a Postgres SSL
    drop here. Without this wrapper, Railway's idle-connection eviction
    on the reaper's first query of the cycle bubbles all the way up to
    Sentry every few minutes, even though the reaper itself recovers on
    the next cycle. One retry with a fresh session swallows the noise.
    """
    last_exc: Exception | None = None
    for attempt in (1, 2):
        try:
            return _reap_all_stuck_inner(threshold_min)
        except OperationalError as exc:
            if not any(m in str(exc) for m in _TRANSIENT_DB_MARKERS):
                raise
            last_exc = exc
            logger.warning(
                "reaper: transient DB error on attempt %d (%s) — retrying",
                attempt, type(exc).__name__,
            )
            time.sleep(0.5)
    # Both attempts hit transient errors. Surface the original so Sentry
    # still has visibility on persistent outages.
    assert last_exc is not None
    raise last_exc


def _reap_all_stuck_inner(threshold_min: int) -> int:
    """One pass. Owns its own DB session — safe to call from a worker
    thread or a scheduled task. Returns the count of reaped jobs.

    Uses pg_try_advisory_lock so when the API scales horizontally
    (Railway > 1 replica), only one instance does the reap pass per
    cycle. Without this, every replica's reaper thread runs in
    parallel — the work is idempotent but it's 2-3× the DB load and
    triplicates the Sentry/email notification noise.

    SQLite (tests) silently no-ops the lock and proceeds — the test
    suite never has competing reapers.
    """
    db = SessionLocal()
    try:
        # Try to take the advisory lock. pg_try_advisory_lock is non-
        # blocking; if another replica already has it, returns false
        # and we skip this cycle entirely. The lock auto-releases on
        # session close (we always close in finally below).
        is_postgres = db.bind.dialect.name == "postgresql"
        if is_postgres:
            from sqlalchemy import text
            got = db.execute(
                text("SELECT pg_try_advisory_lock(:k)"),
                {"k": _REAPER_ADVISORY_LOCK_KEY},
            ).scalar()
            if not got:
                logger.debug(
                    "reaper: another replica holds the advisory lock; "
                    "skipping this cycle",
                )
                return 0

        stuck = find_stuck_jobs(db, threshold_min)
        orphans = find_orphan_polling_jobs(db)
        stalled = find_stalled_renders(db)
        # De-dupe across the three processing-status sweeps. `stuck` (age-
        # based) wins over both, then `orphans` (AI in-flight) wins over
        # `stalled` (last_progress_at). Same root cause "worker dead" so
        # the operator only needs one reaped-notification per job; we just
        # pick the most specific reason.
        stuck_ids = {j.job_id for j in stuck}
        orphans = [j for j in orphans if j.job_id not in stuck_ids]
        orphan_ids = {j.job_id for j in orphans}
        stalled = [j for j in stalled
                   if j.job_id not in stuck_ids and j.job_id not in orphan_ids]
        abandoned = find_abandoned_transcribed(db)
        abandoned_uploads = find_abandoned_uploads(db)
        abandoned_edits = find_abandoned_edits(db)
        stuck_transcriptions = find_stuck_transcriptions(db)
        # Reap abandoned transcribed_pending rows quietly: the user never
        # got a job started, so the failure isn't operator-visible. Just
        # delete the row and clean up the input file.
        #
        # Per-job try/except + commit: a single row that fails to delete
        # (FK, storage hiccup, detached state) must NOT abort the whole
        # sweep and silently leave EVERY abandoned row alive. Observed on
        # staging 2026-05-22: transcribed_pending rows idle 3-6 h were
        # never reaped while the upload sweep kept logging success — a
        # batch failure swallowing the transcribed cleanup. Per-job
        # isolation + a logged reason prevents the silent pile-up.
        _n_tr = _n_up = _n_ed = 0
        for job in abandoned:
            try:
                _delete_abandoned_transcribed(db, job)
                db.commit()
                _n_tr += 1
            except Exception as e:
                db.rollback()
                logger.warning("[REAPER] reap transcribed %s failed: %s", job.job_id, e)
        for job in abandoned_uploads:
            try:
                _delete_abandoned_upload(db, job)
                db.commit()
                _n_up += 1
            except Exception as e:
                db.rollback()
                logger.warning("[REAPER] reap upload %s failed: %s", job.job_id, e)
        # Abandoned edits get reverted (not deleted) — the prior render
        # is still on R2 and the user wants to re-approve or re-try.
        for job in abandoned_edits:
            try:
                revert_abandoned_edit(db, job)
                db.commit()
                _n_ed += 1
            except Exception as e:
                db.rollback()
                logger.warning("[REAPER] revert edit %s failed: %s", job.job_id, e)
        # Stuck transcriptions get flipped to transcription_failed so the
        # editor's "Reintentar" CTA surfaces; the audio sits on R2 untouched
        # so the retry skips the upload entirely. Same per-job isolation
        # as the other transcribed/upload/edit sweeps — a single FK or
        # detached-state hiccup must not stop the whole pass.
        _n_tx = 0
        for job in stuck_transcriptions:
            try:
                reap_stuck_transcription(db, job)
                db.commit()
                _n_tx += 1
            except Exception as e:
                db.rollback()
                logger.warning(
                    "[REAPER] reap transcription %s failed: %s",
                    job.job_id, e,
                )
        if _n_tr:
            logger.info("[REAPER] cleaned up %d abandoned transcribed_pending job(s)", _n_tr)
        if _n_up:
            logger.info("[REAPER] cleaned up %d abandoned awaiting_upload job(s)", _n_up)
        if _n_ed:
            logger.info("[REAPER] reverted %d abandoned edit job(s) back to pending_review", _n_ed)
        if _n_tx:
            logger.warning(
                "[REAPER] flipped %d stuck transcription(s) to transcription_failed",
                _n_tx,
            )
        if not stuck and not orphans and not stalled:
            return 0
        # reap_stuck_job returns False when its in-function race guard
        # (re-fetch + recheck status under FOR UPDATE) detects the worker
        # already completed between the sweep SELECT and now. Filter those
        # out so the audit log / email / Sentry only reports rows we
        # actually mutated.
        stuck    = [j for j in stuck    if reap_stuck_job(db, j, _reason_for(j))]
        orphans  = [j for j in orphans  if reap_stuck_job(db, j, _reason_for_orphan(j))]
        stalled  = [j for j in stalled  if reap_stuck_job(db, j, _reason_for_stalled(j))]
        db.commit()
        # Detach so we can pass to background helpers without keeping the
        # session open. The fields we need are already populated.
        for job in stuck:
            db.expunge(job)
        for job in orphans:
            db.expunge(job)
        for job in stalled:
            db.expunge(job)
        # Merge all three sweeps into one notification batch — operators
        # care about "what got reaped this cycle", not which sweep flagged
        # it. de-dup above already guaranteed no overlap.
        stuck = stuck + orphans + stalled
    finally:
        # Releasing the advisory lock is implicit on session close
        # (Postgres releases all session-scoped locks automatically),
        # but we call pg_advisory_unlock explicitly for clarity and
        # to fail fast if pool reuse ever changes the behaviour.
        if 'is_postgres' in locals() and is_postgres:
            try:
                from sqlalchemy import text
                db.execute(
                    text("SELECT pg_advisory_unlock(:k)"),
                    {"k": _REAPER_ADVISORY_LOCK_KEY},
                )
                db.commit()
            except Exception:
                pass
        db.close()

    # Side-effect notifications happen AFTER the DB commit so a failed
    # email/Sentry call never rolls back a successful reap.
    _sentry_capture(stuck)
    _email_owner(stuck)

    logger.warning(
        "reaper killed %d stuck job(s): %s",
        len(stuck),
        [j.job_id for j in stuck],
    )
    return len(stuck)
