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
        from pipeline import run_pipeline
        # RQ's enqueue() does not accept positional args together with the
        # explicit kwargs= parameter — you have to pass either bare *args/**kwargs
        # or use both args= and kwargs= explicitly. We use the explicit form
        # because we want to forward the caller's **kwargs to the worker.
        # Stretch timeout for ProRes-bearing profiles. The kwargs forwarded
        # to run_pipeline include `delivery_profile` from /generate.
        delivery = (kwargs.get("delivery_profile") or "youtube").lower()
        timeout = JOB_TIMEOUT_UMG if delivery in ("umg", "both") else JOB_TIMEOUT
        rq_job = q.enqueue(
            run_pipeline,
            args=(job_id, mp3_path, artist, style),
            kwargs=kwargs,
            job_timeout=timeout,
            result_ttl=RESULT_TTL,
            failure_ttl=FAILURE_TTL,
            job_id=job_id,  # map RQ id to our job_id for easy lookup
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
    if not PRORES_PREWARM_ENABLED:
        return None
    if file_type not in ("umg_master", "umg_short"):
        logger.warning("[PRORES] prewarm: unsupported file_type %r", file_type)
        return None
    _, _, q_enterprise = _init_redis()
    if q_enterprise is None:
        logger.info("[PRORES] prewarm: queue unavailable; skipping")
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
