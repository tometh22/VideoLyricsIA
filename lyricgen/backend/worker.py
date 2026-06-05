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
import socket
import sys

logger = logging.getLogger("genly.worker")


def _redis_connect_kwargs() -> dict:
    """Connection kwargs for the worker's Redis client (resilience to a
    Railway PRIVATE-NETWORKING blip).

    The worker's main loop does a long BLOCKING ``BLPOP`` to dequeue jobs.
    That makes the timeout story different from the enqueue side
    (queue_jobs.py uses a short ``socket_timeout=5``, fine for its quick
    ping/enqueue ops): a short ``socket_timeout`` here would FIRE SPURIOUSLY
    on every idle BLPOP and break dequeuing, so we deliberately do NOT set
    it. Instead we detect a half-dead connection during the in-flight BLPOP
    via tuned TCP keepalive — the OS probes the peer and tears the socket
    down in ~30-60s, so RQ reconnects in seconds instead of the worker
    silently wedging on a dead socket until Railway SIGKILLs it ~20 min
    later (see WarmOnlyWorker docstring on the hung-parent BLPOP case).

    ``socket_connect_timeout`` only bounds the INITIAL connect, so it's safe
    and useful. ``health_check_interval`` pings idle connections before
    reuse. The TCP_KEEP* options are Linux-only (prod runs Linux); we guard
    them with getattr so importing/running on macOS dev/test is a no-op
    (falls back to OS-default keepalive timing).
    """
    keepalive_opts: dict = {}
    for _opt_name, _opt_val in (
        ("TCP_KEEPIDLE", int(os.environ.get("REDIS_TCP_KEEPIDLE", "30"))),
        ("TCP_KEEPINTVL", int(os.environ.get("REDIS_TCP_KEEPINTVL", "10"))),
        ("TCP_KEEPCNT", int(os.environ.get("REDIS_TCP_KEEPCNT", "3"))),
    ):
        _opt = getattr(socket, _opt_name, None)
        if _opt is not None:
            keepalive_opts[_opt] = _opt_val

    return {
        "socket_connect_timeout": int(os.environ.get("REDIS_SOCKET_CONNECT_TIMEOUT", "5")),
        "socket_keepalive": True,
        "socket_keepalive_options": keepalive_opts,
        "health_check_interval": int(os.environ.get("REDIS_HEALTH_CHECK_INTERVAL", "30")),
    }

from credentials_bootstrap import bootstrap_vertex_credentials
bootstrap_vertex_credentials()

from rq import Worker as _RQWorker


class WarmOnlyWorker(_RQWorker):
    """RQ Worker that NEVER cold-kills an in-flight job.

    Stock RQ escalates a SECOND shutdown signal to a COLD shutdown
    (`request_force_stop` → kills the work-horse + SystemExit). That loses the
    in-flight render whenever a burst of deploys lands a 2nd SIGTERM while we're
    still draining the 1st deploy's render — the exact job-loss agus.cafisi saw
    on a high-deploy day (empirically reproduced 2026-06-03: a double-SIGTERM
    cold-killed a mid-render job, single-SIGTERM let it finish).

    We refuse to escalate: every shutdown signal stays a WARM shutdown, so an
    in-flight render keeps running across a deploy burst instead of being
    cold-killed. The hard caps still apply, in order:
      - the job's own job_timeout (RQ's SIGALRM death-penalty inside the forked
        work-horse, plus monitor_work_horse killing the horse at timeout+60s), and
      - Railway's RAILWAY_SHUTDOWN_TIMEOUT_SECONDS (1200s) SIGKILL of the container.
    So a render with under ~20 min of work LEFT when the burst lands finishes in
    place; one with more left is still SIGKILLed at the grace deadline — but the
    reaper (PR #531) requeues it, so it is RECOVERED, not silently lost. The net
    guarantee is narrow but real: a deploy burst never *silently* throws away a
    render that's making progress (the common, sub-20-min-remaining case finishes;
    the rare long-tail case is requeued).

    Tradeoff: this also removes the operator's ability to cold-kill via a 2nd
    SIGTERM/SIGINT. For a genuinely hung PARENT (e.g. a blocking Redis BLPOP
    during a partition, or a stuck Whisper preload — work the horse's job_timeout
    does NOT cap) the only backstop is Railway's 1200s SIGKILL (and the reaper).
    On prod that's the accepted contract; locally use `kill -9` if needed.

    NOTE: overrides request_force_stop on the *base* Worker only. Do NOT switch
    this process to HerokuWorker / WorkerPool without re-auditing the SIGRTMIN
    cold path (request_force_stop_sigrtmin, rq/worker.py:1610), which this does
    not cover — test_override_is_live_against_rq_api would not catch that drift.

    (worker.py's own SIGTERM handler is also overwritten by RQ's
    `_install_signal_handlers` inside `work()`, which is why no "Received
    signal" line ever appeared in the logs — this subclass is where the real
    burst-deploy protection lives.)
    """

    def request_force_stop(self, signum, frame):  # type: ignore[override]
        logger.warning(
            "[WORKER] extra shutdown signal (%s) ignored — staying in WARM "
            "shutdown so the in-flight job finishes (burst-deploy protection). "
            "Railway's 20-min SIGKILL remains the hard cap.", signum,
        )
        # Intentionally do NOT escalate to cold shutdown: no kill_horse(), no
        # SystemExit. The warm-shutdown flag set by the first signal still makes
        # the worker exit cleanly once the current job completes.


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


_DEFAULT_QUEUES = "transcription,bg_preview,enterprise,default"


def _resolve_queue_names() -> list:
    """Queue names this worker process listens on, in priority order.

    Env-driven (QUEUES, comma-separated) so the SAME image runs as a segmented
    fleet: a small ShortWorker pool (QUEUES=transcription,bg_preview) drains the
    always-short jobs without waiting behind 12-20 min renders, while the render
    pool (QUEUES=enterprise,default) owns the heavy work. RQ priority alone
    doesn't preempt — once all render workers are inside long renders, a short
    transcription waits the full render time; a dedicated pool fixes that.

    Default = all four queues = current single-pool behavior, so shipping this
    is a zero-behavior-change until QUEUES is set per Railway service.
    """
    raw = os.environ.get("QUEUES", _DEFAULT_QUEUES)
    names = [q.strip() for q in raw.split(",") if q.strip()]
    return names or _DEFAULT_QUEUES.split(",")


def main():
    redis_url = os.environ.get("REDIS_URL", "").strip()
    if not redis_url:
        logger.critical("[WORKER] REDIS_URL is required; aborting")
        sys.exit(1)

    # Observability MUST init before the worker loop starts. Without this,
    # every sentry_sdk.capture_exception() in the pipeline / transcription
    # workers and the RQ failure callbacks was a silent no-op in prod —
    # the SDK was never initialized in this process, only in the API
    # (found during the 2026-06-01 UMG-launch hardening audit). Both
    # helpers are no-ops when their env vars are unset, so local dev and
    # CI behave exactly as before.
    from observability import init_logging, init_sentry
    init_logging()
    init_sentry()

    _warn_if_shutdown_grace_too_short()

    from redis import Redis
    from rq import Queue  # the worker itself is WarmOnlyWorker (module-level)

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

    conn = Redis.from_url(redis_url, **_redis_connect_kwargs())
    # Priority order:
    #   1. transcription — corta latencia (~15-20s), debe drenar primero para
    #      que el usuario vea el editor rápido. Si no es prioritaria queda
    #      detrás de un /generate (15-30 min) y arruina la UX que prometimos.
    #   2. bg_preview — Capa C 2026-05-24: pre-gen del background mientras el
    #      operador edita lyrics. Necesita correr en paralelo a transcribe pero
    #      no debe bloquear los renders finales (default). Latencia ~60-120s.
    #   3. enterprise — premium tenants (UMG/OMG) van antes que default.
    #   4. default — todo lo demás.
    # Workers listen in this order; RQ pickup respects it. The set is
    # env-driven (QUEUES) so this image can run as a segmented fleet — see
    # _resolve_queue_names(). Default = all four = current behavior.
    queue_names = _resolve_queue_names()
    queues = [Queue(name, connection=conn) for name in queue_names]
    # WarmOnlyWorker: a burst of deploys (2nd SIGTERM mid-drain) would otherwise
    # COLD-kill the in-flight render. This subclass keeps every shutdown warm so
    # the render finishes. See the class docstring.
    worker = WarmOnlyWorker(queues, connection=conn)

    # NOTE: this handler only covers the brief window BEFORE worker.work()
    # runs — RQ's work() calls _install_signal_handlers() which OVERWRITES
    # these with its own request_stop/request_force_stop. The real burst-deploy
    # protection is WarmOnlyWorker.request_force_stop above, not this handler.
    def _graceful(signum, _frame):
        logger.info("[WORKER] Received signal %s (pre-work); requesting warm stop", signum)
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

    logger.info("[WORKER] Listening on: %s | max_jobs=%s", ", ".join(queue_names), max_jobs)
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
