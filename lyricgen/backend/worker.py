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

from credentials_bootstrap import bootstrap_vertex_credentials
bootstrap_vertex_credentials()

# Audit 2026-05-26: the API process initialized Sentry + structured logging
# but worker.py did neither. Render failures, NameError regressions, OOMs
# in run_pipeline — none of them reported to Sentry. Operators only saw
# them via Railway log search. The 2026-05-26 NameError outage took 17 h
# to find for exactly this reason. Init both before anything else runs.
from observability import init_logging, init_sentry
init_logging()
try:
    from sentry_sdk.integrations.rq import RqIntegration
    init_sentry(integrations=[RqIntegration()])
except ImportError:
    init_sentry()

logger = logging.getLogger("genly.worker")


def _warn_if_shutdown_grace_too_short() -> None:
    """Loud startup warning when Railway's SIGTERM→SIGKILL grace is too
    short to let RQ drain an in-flight render. Without a 20-min grace,
    every redeploy of the Worker service kills any render mid-flight —
    operators see "El servidor se reinició mientras generábamos el
    video" and have to manually retry. See railway.toml comment for the
    incident chain (2026-05-15 first surface, 2026-05-19 re-surfaced when
    two back-to-back PR merges killed the same UMG render twice).

    This is a runtime warning, not a hard failure: workers must still
    start in environments without Railway (local dev, CI) where the
    var is meaningless. Operators reading Railway logs will see the
    WARNING line and know to set the dashboard variable.
    """
    raw = os.environ.get("RAILWAY_SHUTDOWN_TIMEOUT_SECONDS", "").strip()
    # Only nag when we look like we're running on Railway (the platform
    # injects RAILWAY_ENVIRONMENT). On local dev there is no SIGTERM
    # dance to worry about.
    if not os.environ.get("RAILWAY_ENVIRONMENT", "").strip():
        return
    try:
        grace_s = int(raw) if raw else 0
    except ValueError:
        grace_s = 0
    # 20 min = the value documented in railway.toml as required for UMG-grade
    # renders. Anything shorter and a SIGKILL will land mid-encode.
    REQUIRED_GRACE_S = 1200
    if grace_s < REQUIRED_GRACE_S:
        logger.warning(
            "[WORKER] RAILWAY_SHUTDOWN_TIMEOUT_SECONDS=%r is below the %ds "
            "needed to drain UMG renders cleanly. Every redeploy of the "
            "Worker service will SIGKILL the in-flight render and surface "
            "\"servidor se reinició\" to operators. Fix: Railway dashboard "
            "→ Worker service → Variables → set "
            "RAILWAY_SHUTDOWN_TIMEOUT_SECONDS=1200. (Cannot be set via "
            "railway.toml.) See railway.toml comment for full context.",
            raw or "<unset>", REQUIRED_GRACE_S,
        )


def main():
    redis_url = os.environ.get("REDIS_URL", "").strip()
    if not redis_url:
        logger.critical("[WORKER] REDIS_URL is required; aborting")
        sys.exit(1)

    _warn_if_shutdown_grace_too_short()

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
    # Priority order:
    #   1. transcription — corta latencia (~15-20s), debe drenar primero para
    #      que el usuario vea el editor rápido. Si no es prioritaria queda
    #      detrás de un /generate (15-30 min) y arruina la UX que prometimos.
    #   2. bg_preview — Capa C 2026-05-24: pre-gen del background mientras el
    #      operador edita lyrics. Necesita correr en paralelo a transcribe pero
    #      no debe bloquear los renders finales (default). Latencia ~60-120s.
    #   3. enterprise — premium tenants (UMG/OMG) van antes que default.
    #   4. default — todo lo demás.
    # Workers listen in this order; RQ pickup respects it.
    queues = [
        Queue("transcription", connection=conn),
        Queue("bg_preview", connection=conn),
        Queue("enterprise", connection=conn),
        Queue("default", connection=conn),
    ]
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

    logger.info("[WORKER] Listening on: transcription, bg_preview, enterprise, default | max_jobs=%s", max_jobs)
    # CRITICAL (incident 2026-05-26): `with_scheduler=True` is required to
    # process retry-scheduled jobs. Every `enqueue_*` helper uses
    # `Retry(interval=N)` (queue_jobs.py:366/446/520) for survival across
    # Railway worker death. RQ 1.16's `Job.retry()` (rq/job.py:1498) puts
    # the retry into ScheduledJobRegistry at NOW+interval — and ONLY a
    # running scheduler moves it back to the live queue when due.
    #
    # With `with_scheduler=False` and no external rqscheduler service, every
    # job that hit Retry stayed stranded in ScheduledJobRegistry forever:
    # the Postgres row froze in `transcribing` / `processing`, the on_failure
    # callback never fired (retries weren't exhausted, just stranded), and
    # the operator saw "En cola" / "Generando" indefinitely. Triggered by
    # agus.cafisi (omg) reporting 3 stuck jobs at 46m/2h/2h and "varios
    # generados en cola sin avanzar".
    #
    # RQScheduler uses Redis locks so only one of the 7 replicas runs the
    # active scheduler at a time; the others stand by and take over if the
    # holder dies. No coordination required here.
    worker.work(with_scheduler=True, max_jobs=max_jobs)


if __name__ == "__main__":
    main()
