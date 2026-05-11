"""RQ worker entrypoint.

Run in a separate process from the API:

    REDIS_URL=redis://localhost:6379 python worker.py

Pre-loads the Whisper model at startup so the first job doesn't pay the
model-load cost, and installs SIGTERM/SIGINT handlers so an in-flight job
finishes cleanly when the container is recycled.
"""

import os
import signal
import sys

from credentials_bootstrap import bootstrap_vertex_credentials
bootstrap_vertex_credentials()


def main():
    redis_url = os.environ.get("REDIS_URL", "").strip()
    if not redis_url:
        print("[WORKER] REDIS_URL is required; aborting")
        sys.exit(1)

    from redis import Redis
    from rq import Queue, Worker

    # Warm the Whisper model so the first job does not pay the load cost.
    # SKIP this when OPENAI_API_KEY is set — transcription routes through the
    # OpenAI Whisper API and the local 1.5 GB model is just dead weight that
    # increases worker RAM and starts the container into immediate OOM
    # territory on small instances.
    if os.environ.get("OPENAI_API_KEY", "").strip():
        print("[WORKER] OPENAI_API_KEY set; skipping local Whisper preload")
    else:
        try:
            from pipeline import _get_whisper_model
            _get_whisper_model("turbo")
            print("[WORKER] Whisper model preloaded")
        except Exception as e:
            print(f"[WORKER] Whisper preload failed ({e}); will load on first job")

    conn = Redis.from_url(redis_url)
    # Enterprise queue first so premium tenants get priority.
    queues = [Queue("enterprise", connection=conn), Queue("default", connection=conn)]
    # Heartbeat (job_monitoring_interval) controls how often the worker
    # writes its "I'm alive" timestamp to Redis. The default 30 s is
    # fine; the related value that matters for deploy resilience is the
    # worker_ttl — how long after a missed heartbeat RQ declares the
    # worker dead. RQ defaults that to job_monitoring_interval * 12 =
    # 360 s (~6 min), which is the "deploy zombie window" users see in
    # the UI: jobs that look "processing" but no one's working on them.
    # We pin worker_ttl to 90 s so cleanup_ghosts runs ~90 s after a
    # worker death instead of 6 min — most users won't notice the gap,
    # and the new orphan-no-worker reaper sweep (reaper.py) is the
    # backstop if RQ's own cleanup misses.
    monitoring_interval = int(os.environ.get("RQ_JOB_MONITORING_INTERVAL", "30"))
    worker_ttl = int(os.environ.get("RQ_WORKER_TTL", "90"))
    worker = Worker(
        queues,
        connection=conn,
        job_monitoring_interval=monitoring_interval,
        worker_ttl=worker_ttl,
    )

    def _graceful(signum, _frame):
        print(f"[WORKER] Received signal {signum}; requesting stop after current job")
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

    print(f"[WORKER] Listening on: enterprise, default | max_jobs={max_jobs}")
    worker.work(with_scheduler=False, max_jobs=max_jobs)


if __name__ == "__main__":
    main()
