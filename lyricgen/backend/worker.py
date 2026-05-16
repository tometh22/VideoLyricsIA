"""RQ worker entrypoint.

Run in a separate process from the API:

    REDIS_URL=redis://localhost:6379 python worker.py

Pre-loads the Whisper model at startup so the first job doesn't pay the
model-load cost, and installs SIGTERM/SIGINT handlers so an in-flight job
finishes cleanly when the container is recycled.
"""

import logging
import os
import signal
import sys

logger = logging.getLogger("genly.worker")

from credentials_bootstrap import bootstrap_vertex_credentials
bootstrap_vertex_credentials()


def main():
    redis_url = os.environ.get("REDIS_URL", "").strip()
    if not redis_url:
        logger.critical("[WORKER] REDIS_URL is required; aborting")
        sys.exit(1)

    from redis import Redis
    from rq import Queue, Worker

    # Warm the Whisper model so the first job does not pay the load cost.
    # SKIP this when OPENAI_API_KEY is set — transcription routes through the
    # OpenAI Whisper API and the local 1.5 GB model is just dead weight that
    # increases worker RAM and starts the container into immediate OOM
    # territory on small instances.
    if os.environ.get("OPENAI_API_KEY", "").strip():
        logger.info("[WORKER] OPENAI_API_KEY set; skipping local Whisper preload")
    else:
        try:
            from pipeline import _get_whisper_model
            _get_whisper_model("turbo")
            logger.info("[WORKER] Whisper model preloaded")
        except Exception as e:
            logger.warning("[WORKER] Whisper preload failed (%s); will load on first job", e)

    conn = Redis.from_url(redis_url)
    # Enterprise queue first so premium tenants get priority.
    queues = [Queue("enterprise", connection=conn), Queue("default", connection=conn)]
    worker = Worker(queues, connection=conn)

    def _graceful(signum, _frame):
        logger.info("[WORKER] Received signal %s; requesting stop after current job", signum)
        worker.request_stop(signum, _frame)

    signal.signal(signal.SIGTERM, _graceful)
    signal.signal(signal.SIGINT, _graceful)

    # moviepy 1.0.3 leaks memory between renders — VideoFileClip and friends
    # are not fully released even when user code calls .close(). Long-lived
    # workers degrade after ~10–15 jobs and end up hanging mid-encode at
    # video/40%, requiring a manual restart.
    #
    # Standard production mitigation: cap the worker's lifetime at N jobs,
    # then exit cleanly. Railway's restart policy spawns a replacement in
    # ~30 s. RQ leaves un-claimed jobs in the queue, so nothing is lost —
    # the next worker picks them up.
    #
    # WORKER_MAX_JOBS=10 (default) ≈ 100 min of healthy work between recycles.
    # Lower (e.g. 5) for very long renders that burn more memory per job.
    max_jobs_env = os.environ.get("WORKER_MAX_JOBS", "10").strip()
    try:
        max_jobs = int(max_jobs_env) if max_jobs_env else None
        if max_jobs is not None and max_jobs <= 0:
            max_jobs = None
    except ValueError:
        max_jobs = 10

    logger.info("[WORKER] Listening on: enterprise, default | max_jobs=%s", max_jobs)
    worker.work(with_scheduler=False, max_jobs=max_jobs)


if __name__ == "__main__":
    main()
