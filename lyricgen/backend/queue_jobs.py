"""Redis-backed job queue.

Replaces the fire-and-forget threading.Thread model with a durable queue that
survives API restarts and bounds concurrency. Two queues by priority:

    enterprise  -> UMG and any tenant with plan == "unlimited"
    default     -> everyone else

Workers pick enterprise first. If Redis is unavailable (e.g. local dev without
Redis running), the helpers fall back to threading.Thread so the dev loop
still works — production must set REDIS_URL.
"""

import os
import threading

REDIS_URL = os.environ.get("REDIS_URL", "").strip()
JOB_TIMEOUT = int(os.environ.get("JOB_TIMEOUT_SECONDS", "2700"))  # 45 min
RESULT_TTL = int(os.environ.get("JOB_RESULT_TTL_SECONDS", "86400"))  # 24 h
FAILURE_TTL = int(os.environ.get("JOB_FAILURE_TTL_SECONDS", "604800"))  # 7 d

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
        rq_job = q.enqueue(
            run_pipeline,
            job_id, mp3_path, artist, style,
            kwargs=kwargs,
            job_timeout=JOB_TIMEOUT,
            result_ttl=RESULT_TTL,
            failure_ttl=FAILURE_TTL,
            job_id=job_id,  # map RQ id to our job_id for easy lookup
        )
        return rq_job.id

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
