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
    try:
        from pipeline import _get_whisper_model
        _get_whisper_model("turbo")
        print("[WORKER] Whisper model preloaded")
    except Exception as e:
        print(f"[WORKER] Whisper preload failed ({e}); will load on first job")

    conn = Redis.from_url(redis_url)
    # Enterprise queue first so premium tenants get priority.
    queues = [Queue("enterprise", connection=conn), Queue("default", connection=conn)]
    worker = Worker(queues, connection=conn)

    def _graceful(signum, _frame):
        print(f"[WORKER] Received signal {signum}; requesting stop after current job")
        worker.request_stop(signum, _frame)

    signal.signal(signal.SIGTERM, _graceful)
    signal.signal(signal.SIGINT, _graceful)

    print(f"[WORKER] Listening on: enterprise, default")
    worker.work(with_scheduler=False)


if __name__ == "__main__":
    main()
