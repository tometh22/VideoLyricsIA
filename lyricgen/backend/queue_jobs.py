"""Redis-backed job queue.

Replaces the fire-and-forget threading.Thread model with a durable queue that
survives API restarts and bounds concurrency. Two queues by priority:

    enterprise  -> UMG and any tenant with plan == "unlimited"
    default     -> everyone else

Workers pick enterprise first. If Redis is unavailable AND we're not in
production, the helpers fall back to threading.Thread so the dev loop still
works. Production refuses to start the fallback — silently turning the API
into a fire-and-forget thread runner on a transient Redis blip would lose
durability, concurrency caps, and timeouts in the worst possible moment.
"""

import logging
import os
import threading

logger = logging.getLogger("genly.queue")

REDIS_URL = os.environ.get("REDIS_URL", "").strip()
JOB_TIMEOUT = int(os.environ.get("JOB_TIMEOUT_SECONDS", "2700"))  # 45 min (YouTube)
# RQ auto-retry on worker death (Railway redeploy, OOM, hard signal).
# RQ moves orphaned jobs from StartedJobRegistry to FailedJobRegistry
# via cleanup_ghosts() on the next worker boot, raising AbandonedJobError.
# Retry handles that path. 1 retry is enough — if both attempts die, the
# infrastructure is unhealthy and the failure_callback marks the row as
# error with a retry-button hint instead of looping forever.
PIPELINE_RETRY_MAX = int(os.environ.get("PIPELINE_RETRY_MAX", "1"))
# Backoff between attempt 1 → attempt 2. 30 s gives the new worker pod
# time to come up after a Railway redeploy before we re-claim the job.
PIPELINE_RETRY_INTERVAL_S = int(os.environ.get("PIPELINE_RETRY_INTERVAL_S", "30"))
# UMG / both renders chain MP4 + ProRes + Short + Thumb + Veo retries.
# A 7-min track with a fresh Veo gen + 2-3 retry rounds + ProRes encode
# + 1.5GB R2 multipart upload can creep past 45min. Give it 90min.
JOB_TIMEOUT_UMG = int(os.environ.get("JOB_TIMEOUT_UMG_SECONDS", "5400"))
# Prewarm transcode timeout — a 7-min song's ProRes is ~2 GB; ffmpeg
# usually finishes in 1-3 min. 15 min is plenty of headroom and still
# bounds runaway processes.
PRORES_PREWARM_TIMEOUT = int(os.environ.get("PRORES_PREWARM_TIMEOUT_SECONDS", "900"))
RESULT_TTL = int(os.environ.get("JOB_RESULT_TTL_SECONDS", "86400"))  # 24 h
FAILURE_TTL = int(os.environ.get("JOB_FAILURE_TTL_SECONDS", "604800"))  # 7 d
_ENVIRONMENT = os.environ.get("ENVIRONMENT", "production").lower().strip() or "production"

# Pre-warm the ProRes deliverables in a background worker job as soon as
# the pipeline finishes the MP4 render. Trade-off: gasta ffmpeg en jobs
# que tal vez nunca se descarguen en ProRes, pero le ahorra a UMG el
# 60-120 s wait en el primer click. Default ON since UMG is the only
# tenant currently triggering the umg/both delivery_profile.
PRORES_PREWARM_ENABLED = os.environ.get("PRORES_PREWARM", "1").lower() not in ("0", "false", "no")

# Backpressure: when the enterprise queue already has more than this
# many jobs waiting, skip new prewarm enqueues. The lazy /download path
# (with its 202+Retry-After contract) handles the wait gracefully, so
# skipping is strictly better than letting the queue grow unbounded
# and starve render jobs from the same UMG batch.
PRORES_PREWARM_MAX_QUEUE_DEPTH = int(
    os.environ.get("PRORES_PREWARM_MAX_QUEUE_DEPTH", "15")
)

# Counter exposed via /health so operators can see when prewarm is
# being throttled. Process-local — fine for single-instance, ok-ish for
# horizontal scale (each instance reports its own count).
prewarm_skipped_total = 0
prewarm_enqueued_total = 0

_redis = None
_queue_default = None
_queue_enterprise = None


def _init_redis():
    """Lazy-init Redis + RQ queues. Returns (redis, default_q, enterprise_q) or
    (None, None, None) if Redis is not configured or unreachable."""
    global _redis, _queue_default, _queue_enterprise
    if _queue_default is not None:
        return _redis, _queue_default, _queue_enterprise
    if not REDIS_URL:
        return None, None, None
    try:
        from redis import Redis
        from rq import Queue
        _redis = Redis.from_url(REDIS_URL)
        _redis.ping()
        _queue_default = Queue("default", connection=_redis)
        _queue_enterprise = Queue("enterprise", connection=_redis)
        return _redis, _queue_default, _queue_enterprise
    except Exception as e:
        print(f"[QUEUE] Redis init failed ({e}); falling back to threads")
        return None, None, None


def _pick_queue(plan: str):
    """Enterprise queue for premium plans, default otherwise."""
    _, q_default, q_enterprise = _init_redis()
    if q_default is None:
        return None
    if plan in ("unlimited", "enterprise"):
        return q_enterprise
    return q_default


def pipeline_failure_callback(job, connection, type_, value, traceback) -> None:
    """RQ on_failure hook for run_pipeline. Fires when retries are
    exhausted (i.e. the job is permanently dead). Updates the Postgres
    row so the user sees a clear "Reintentar sin re-subir" affordance
    instead of a frozen "Generando" or a generic infra error.

    The signature matches RQ's failure-callback contract: (job, connection,
    type_, value, traceback). type_/value identify the failure class —
    AbandonedJobError means a worker died mid-render (deploy/OOM/SIGKILL),
    everything else is a real exception inside the pipeline.

    Best-effort: any exception in here is swallowed so RQ's own failure
    bookkeeping still completes — a noisy callback that breaks the
    failure path is worse than no callback at all.
    """
    try:
        from database import Job as JobModel, SessionLocal
        # RQ's job.id == our job_id (we map them 1:1 in enqueue_pipeline).
        rq_job_id = getattr(job, "id", None) or ""
        if not rq_job_id:
            return
        is_abandoned = "AbandonedJobError" in (type_.__name__ if type_ else "")
        if is_abandoned:
            err_msg = (
                "El servidor se reinició mientras generábamos el video y "
                "los reintentos automáticos también fallaron. Tu MP3 sigue "
                "guardado: apretá \"Reintentar sin re-subir\"."
            )
        else:
            # Real exception from inside run_pipeline. Surface a short
            # version of the message to the user (the full traceback is
            # in Sentry / worker logs). Keep it under 500 chars so it
            # fits the UI error box without truncation surprises.
            tb_msg = str(value)[:400] if value else (type_.__name__ if type_ else "error")
            err_msg = f"El render falló tras reintentos: {tb_msg}"
        db = SessionLocal()
        try:
            row = db.query(JobModel).filter(JobModel.job_id == rq_job_id).first()
            if row is None:
                return
            # Don't clobber a terminal state — if the pipeline managed to
            # write status=done/pending_review before the worker died on
            # a cleanup step, leave it.
            if row.status in ("processing", "queued"):
                row.status = "error"
                row.error = err_msg
                from datetime import datetime, timezone
                row.completed_at = datetime.now(timezone.utc)
                db.commit()
        finally:
            db.close()
    except Exception as e:  # pragma: no cover
        logger.warning("pipeline_failure_callback failed: %s", e)


def enqueue_pipeline(
    job_id: str,
    mp3_path: str,
    artist: str,
    style: str,
    plan: str = "100",
    **kwargs,
) -> str:
    """Enqueue a run_pipeline job. Returns RQ job id (or 'thread:<job_id>' in
    the Redis-less fallback path)."""
    q = _pick_queue(plan)
    if q is not None:
        from rq import Retry
        from pipeline import run_pipeline
        # RQ's enqueue() does not accept positional args together with the
        # explicit kwargs= parameter — you have to pass either bare *args/**kwargs
        # or use both args= and kwargs= explicitly. We use the explicit form
        # because we want to forward the caller's **kwargs to the worker.
        # Stretch timeout for ProRes-bearing profiles. The kwargs forwarded
        # to run_pipeline include `delivery_profile` from /generate.
        delivery = (kwargs.get("delivery_profile") or "youtube").lower()
        timeout = JOB_TIMEOUT_UMG if delivery in ("umg", "both") else JOB_TIMEOUT
        # Retry on worker-death (Railway redeploy/OOM/SIGKILL).
        # run_pipeline restarts cleanly: its first line resets the DB
        # row (status, current_step, progress) so a second attempt picks
        # up from scratch as if it were a fresh enqueue. Veo backgrounds
        # are cached in R2 by prompt hash, so the retry usually skips
        # re-generating the bg and re-uses the cached clip — only the
        # pipeline steps that happened after Veo (render, encode, R2
        # upload) actually re-execute. See pipeline.py for the cache
        # lookup in the [BG] Veo cache STORED path.
        retry = Retry(max=PIPELINE_RETRY_MAX, interval=PIPELINE_RETRY_INTERVAL_S)
        rq_job = q.enqueue(
            run_pipeline,
            args=(job_id, mp3_path, artist, style),
            kwargs=kwargs,
            job_timeout=timeout,
            result_ttl=RESULT_TTL,
            failure_ttl=FAILURE_TTL,
            job_id=job_id,  # map RQ id to our job_id for easy lookup
            retry=retry,
            on_failure=pipeline_failure_callback,
        )
        return rq_job.id

    # Redis-less path. In production this would silently bypass JOB_TIMEOUT,
    # concurrency caps, and durability — refuse instead and let the
    # operator fix the Redis dependency.
    if _ENVIRONMENT == "production":
        logger.error(
            "Refusing to enqueue %s via thread fallback: Redis is required "
            "in production but unreachable.", job_id,
        )
        raise RuntimeError(
            "Job queue unavailable: Redis is required in production. "
            "Check REDIS_URL and the redis service health."
        )

    # Dev fallback: same thread model as before.
    from pipeline import run_pipeline
    t = threading.Thread(
        target=run_pipeline,
        args=(job_id, mp3_path, artist, style),
        kwargs=kwargs,
        daemon=True,
    )
    t.start()
    return f"thread:{job_id}"


def enqueue_prores_prewarm(job_id: str, file_type: str) -> str | None:
    """Schedule the ProRes transcode for `job_id` on the enterprise queue.

    Called from run_pipeline right before the job flips to "done" when
    delivery_profile is umg/both and PRORES_PREWARM is on. The handler
    is `prores.prewarm_prores`, which wraps `ensure_prores_exists` with
    DB lookup. Idempotent against the lazy /download path: whichever
    finishes first wins the os.replace.

    Returns the RQ job id, or None when prewarm is disabled or Redis
    unreachable (we never raise — prewarm is best-effort by design).
    """
    global prewarm_skipped_total, prewarm_enqueued_total
    if not PRORES_PREWARM_ENABLED:
        return None
    if file_type not in ("umg_master", "umg_short"):
        logger.warning("[PRORES] prewarm: unsupported file_type %r", file_type)
        return None
    _, _, q_enterprise = _init_redis()
    if q_enterprise is None:
        logger.info("[PRORES] prewarm: queue unavailable; skipping")
        return None
    # Backpressure: if the enterprise queue is already deep, skip the
    # prewarm. The lazy /download path will produce the .mov when UMG
    # actually clicks (with the toast/poll UX). Deep queue = many UMG
    # batch jobs landing concurrently; better to keep the queue moving
    # for renders than to pile prewarms behind them.
    try:
        depth = q_enterprise.count
    except Exception:
        depth = 0
    if depth > PRORES_PREWARM_MAX_QUEUE_DEPTH:
        prewarm_skipped_total += 1
        logger.warning(
            "[PRORES] prewarm: queue depth %d > %d; skipping prewarm for %s/%s "
            "(lazy /download will handle it on first click)",
            depth, PRORES_PREWARM_MAX_QUEUE_DEPTH, job_id, file_type,
        )
        return None
    rq_job = q_enterprise.enqueue(
        "prores.prewarm_prores",
        args=(job_id, file_type),
        job_timeout=PRORES_PREWARM_TIMEOUT,
        result_ttl=RESULT_TTL,
        failure_ttl=FAILURE_TTL,
        # Use a deterministic id so an inadvertent double-enqueue is a
        # no-op (RQ dedupes by job_id within a queue).
        job_id=f"prewarm:{job_id}:{file_type}",
    )
    prewarm_enqueued_total += 1
    return rq_job.id


def enqueue_edit(
    job_id: str,
    edit_type: str,
    edit_params: dict,
    plan: str = "100",
) -> str:
    """Enqueue a run_edit_pipeline job (partial re-render).

    Uses the same queue priority logic as enqueue_pipeline. Typography
    edits with no/none motion finish in ~5 min, but the per-frame
    position callable used by subtle/float motion blows up moviepy's
    compositing — a 4-min song with 60+ lyric lines and motion enabled
    can run 30-40 min in the video step alone (see pipeline.py
    _text_position_func — TODO: rewrite text layer with ffmpeg overlay
    filters where per-frame motion is essentially free). Background
    regenerations also add the Veo step. The original 20-min budget was
    too tight for long songs; we now match the main pipeline's
    YouTube-only allowance to keep the worst-case edit alive.
    """
    q = _pick_queue(plan)
    if q is not None:
        from pipeline import run_edit_pipeline
        rq_job = q.enqueue(
            run_edit_pipeline,
            args=(job_id, edit_type, edit_params),
            # 60 min — covers worst-case long-song edits with motion enabled
            # until we land the ffmpeg-overlay rewrite.
            job_timeout=3600,
            result_ttl=RESULT_TTL,
            failure_ttl=FAILURE_TTL,
            job_id=f"edit:{job_id}",  # deterministic — double-click deduped
        )
        return rq_job.id

    if _ENVIRONMENT == "production":
        logger.error(
            "Refusing to enqueue edit %s via thread fallback: Redis required.", job_id,
        )
        raise RuntimeError("Job queue unavailable: Redis is required in production.")

    from pipeline import run_edit_pipeline
    t = threading.Thread(
        target=run_edit_pipeline,
        args=(job_id, edit_type, edit_params),
        daemon=True,
    )
    t.start()
    return f"thread:edit:{job_id}"


def enqueue_drive_delivery(transfer_id: str, plan: str = "100") -> str:
    """Encola una transferencia R2 → Google Drive en el worker.

    El worker corre `drive_uploader.run_drive_delivery(transfer_id)`
    que lee el resto del estado de la DB (user_id, job_id, file_type)
    desde la row drive_transfers — esto mantiene la signature simple
    y permite que el worker se reanude tras un crash sin necesitar
    re-pasar args.

    Timeout 60 min: un ProRes de 16 GB a 500 Mbps tarda ~4 min, pero
    si Drive rate-limita o la conexión cloud↔cloud va lenta podría
    estirar a 30-40 min. 60 min da headroom sin permitir colgados.

    Usa la enterprise queue para no competir con render jobs comunes —
    el operador que clickea "Guardar en Drive" típicamente tiene
    delivery_profile=umg/both (UMG plan).
    """
    _, _, q_enterprise = _init_redis()
    if q_enterprise is None:
        if _ENVIRONMENT == "production":
            logger.error(
                "Refusing to enqueue drive_delivery via thread fallback: "
                "Redis required in production."
            )
            raise RuntimeError(
                "Job queue unavailable: Redis is required in production."
            )
        # Dev fallback
        from drive_uploader import run_drive_delivery
        t = threading.Thread(
            target=run_drive_delivery, args=(transfer_id,), daemon=True,
        )
        t.start()
        return f"thread:drive:{transfer_id}"

    rq_job = q_enterprise.enqueue(
        "drive_uploader.run_drive_delivery",
        args=(transfer_id,),
        job_timeout=3600,  # 60 min — ver docstring arriba
        result_ttl=RESULT_TTL,
        failure_ttl=FAILURE_TTL,
        # Deterministic id: un mismo transfer_id se enqueue solo una vez
        # (RQ dedupes). El operador puede crear N transfers distintos.
        job_id=f"drive:{transfer_id}",
    )
    return rq_job.id


def queue_depth() -> dict:
    """Return {'default': n, 'enterprise': n, 'backend': 'redis'|'threads'}."""
    _, q_default, q_enterprise = _init_redis()
    if q_default is None:
        return {"default": 0, "enterprise": 0, "backend": "threads"}
    return {
        "default": len(q_default),
        "enterprise": len(q_enterprise),
        "backend": "redis",
    }
