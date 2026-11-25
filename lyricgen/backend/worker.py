"""RQ worker entrypoint.

Run in a separate process from the API:

    REDIS_URL=redis://localhost:6379 python worker.py

Whisper is loaded on first use unless WHISPER_PRELOAD=1 (interactive and
batch-short workers only). SIGTERM/SIGINT handlers let an in-flight job
finish cleanly when the container is recycled.
"""

import json
import logging
import os
import signal
import socket
import sys
import threading
import time

logger = logging.getLogger("genly.worker")


def _start_local_outputs_cleanup() -> None:
    """Delete stale files from this worker's own isolated filesystem."""

    def _loop():
        # Let startup/imports settle, then sweep locally once per hour.
        time.sleep(120)
        while True:
            try:
                from scripts.cleanup_old_outputs import cleanup
                cleanup()
            except Exception:
                logger.exception("[WORKER] local outputs cleanup failed")
            time.sleep(3600)

    threading.Thread(
        target=_loop, daemon=True, name="worker-outputs-cleanup",
    ).start()


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


_DEFAULT_RELEASE_HEARTBEAT_TTL_SECONDS = 120


def _release_heartbeat_ttl_seconds() -> int:
    """Return a safe TTL for the release/protocol metadata key."""
    raw = os.environ.get(
        "WORKER_RELEASE_HEARTBEAT_TTL_SECONDS",
        str(_DEFAULT_RELEASE_HEARTBEAT_TTL_SECONDS),
    )
    try:
        return max(int(raw), 30)
    except (TypeError, ValueError):
        logger.warning(
            "[WORKER] invalid WORKER_RELEASE_HEARTBEAT_TTL_SECONDS=%r; "
            "using %ds",
            raw,
            _DEFAULT_RELEASE_HEARTBEAT_TTL_SECONDS,
        )
        return _DEFAULT_RELEASE_HEARTBEAT_TTL_SECONDS


def _worker_shutdown_requested(worker) -> bool:
    return bool(
        getattr(worker, "_stop_requested", False)
        or getattr(worker, "_shutdown_requested_date", None) is not None
    )


def _sleep_until_shutdown(worker, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _worker_shutdown_requested(worker):
            return
        time.sleep(min(0.1, deadline - time.monotonic()))


def _queue_length(connection, name: str) -> int:
    from rq import Queue
    try:
        return int(Queue(name, connection=connection).count)
    except Exception:
        return 0


def _head_inputs_ready(queue, job_cls, ready) -> bool:
    if queue is None:
        return False
    try:
        ids = queue.job_ids
        if not ids:
            return False
        job = job_cls.fetch(ids[0], connection=queue.connection)
    except Exception:
        return False
    return bool(ready(job))


def _whisper_preload_enabled() -> bool:
    """Default off: the model loads on the first job that needs it.

    Interactive (short) and batch-short workers set WHISPER_PRELOAD=1.
    Render, ProRes and quality workers must not pay the 1.5 GB load.
    """
    return os.environ.get("WHISPER_PRELOAD", "0").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _maybe_preload_whisper(loader) -> str:
    """Return skipped, skipped_api, loaded, or failed. ``loader`` is injected."""
    if not _whisper_preload_enabled():
        logger.info("[WORKER] WHISPER_PRELOAD off; Whisper loads on first job")
        return "skipped"
    if os.environ.get("OPENAI_API_KEY", "").strip():
        logger.info("[WORKER] OPENAI_API_KEY set; skipping local Whisper preload")
        return "skipped_api"
    try:
        loader()
    except Exception as exc:
        logger.warning(
            "[WORKER] Whisper preload failed (%s); will load on first job", exc,
        )
        return "failed"
    logger.info("[WORKER] Whisper model preloaded")
    return "loaded"


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

    def dequeue_job_and_maintain_ttl(  # type: ignore[override]
        self, timeout, max_idle_time=None,
    ):
        """Dedicated ProRes workers dequeue only when the host slot allows it.

        Stock RQ would pop a prewarm job and the job would then block on a
        lock. That occupied the worker and hid portal jobs still queued in
        Redis. Here the process sleeps *without* a job until a portal export
        can run, or until prewarm is allowed. An encode-ready job keeps the
        flock from the moment it is dequeued until the horse exits, so a
        second worker cannot steal it and defer in a loop. The download
        still runs without the flock.
        """
        from prores_capacity import gate_enabled_for

        if not gate_enabled_for(self.queue_names()):
            return super().dequeue_job_and_maintain_ttl(timeout, max_idle_time)
        return self._dequeue_prores_gated()

    def _dequeue_prores_gated(self):
        from prores_capacity import (
            clear_portal_demand, decide_admission, host_portal_waiting,
            max_concurrency, portal_queue_name, prewarm_queue_name,
            refresh_portal_markers, rq_job_inputs_ready, try_acquire_slot,
        )
        from rq.job import Job

        poll = 0.5
        while True:
            if _worker_shutdown_requested(self):
                return None
            try:
                self.heartbeat()
                by_name = {queue.name: queue for queue in self.queues}
                portal_name = portal_queue_name()
                prewarm_name = prewarm_queue_name()
                portal_n = _queue_length(self.connection, portal_name)
                prewarm_n = _queue_length(self.connection, prewarm_name)
                portal_queue = by_name.get(portal_name)
                if portal_n and portal_queue is not None:
                    try:
                        refresh_portal_markers(portal_queue.job_ids[:100])
                    except Exception:
                        logger.warning("[PRORES] could not refresh portal markers", exc_info=True)
                limit = max_concurrency()
                decision = decide_admission(
                    gate_enabled=True,
                    portal_queued=portal_n,
                    prewarm_queued=prewarm_n,
                    host_portal_waiting=host_portal_waiting(),
                    portal_head_ready=_head_inputs_ready(portal_queue, Job, rq_job_inputs_ready),
                    prewarm_head_ready=_head_inputs_ready(
                        by_name.get(prewarm_name), Job, rq_job_inputs_ready,
                    ),
                    concurrency_limit=limit,
                    # The flock, not this hint, is the real cap. slots_free=1
                    # asks decide() for an encode; try_acquire_slot confirms it.
                    slots_free=None if limit is None else 1,
                )
                if decision.action == "wait":
                    _sleep_until_shutdown(self, poll)
                    continue
                target_name = portal_name if decision.queue == "portal" else prewarm_name
                if target_name not in by_name:
                    # This process does not consume that class. Leave the job
                    # queued for a worker that does.
                    _sleep_until_shutdown(self, poll)
                    continue
                if decision.takes_slot:
                    # Hold the permit across the horse. Releasing it here let
                    # every other worker pop the same ready job, fail to
                    # re-acquire, and requeue with an error traceback.
                    # The fd stays in this process (CLOEXEC); the horse sees
                    # PRORES_SLOT_HELD and does not take a second lock.
                    # execute_job releases it, which is after ffmpeg and the
                    # upload that follows in the same horse.
                    slot = try_acquire_slot()
                    if slot is None:
                        _sleep_until_shutdown(self, poll)
                        continue
                    self._prores_slot = slot
                    os.environ["PRORES_SLOT_HELD"] = "1"
                result = self.queue_class.dequeue_any(
                    [by_name[target_name]],
                    None,
                    connection=self.connection,
                    job_class=self.job_class,
                    serializer=self.serializer,
                    death_penalty_class=self.death_penalty_class,
                )
                if result is None:
                    self._release_prores_slot()
                    _sleep_until_shutdown(self, poll)
                    continue
                job, queue = result
                ready = rq_job_inputs_ready(job)
                if self._prores_slot is not None and not ready:
                    # The head we probed was encode-ready, but the job we
                    # actually popped still needs its source.
                    self._release_prores_slot()
                if queue.name == portal_name and not ready:
                    # A download is not waiting for the CPU. Leave the slot
                    # free for the other deployment's prewarm.
                    clear_portal_demand(getattr(job, "id", "") or "")
                try:
                    self.reorder_queues(reference_queue=queue)
                except Exception:
                    logger.warning("[PRORES] queue reorder failed", exc_info=True)
                return job, queue
            except Exception:
                logger.exception("[PRORES] admission loop failed; retrying")
                self._release_prores_slot()
                _sleep_until_shutdown(self, poll)

    def _release_prores_slot(self) -> None:
        slot = getattr(self, "_prores_slot", None)
        self._prores_slot = None
        os.environ.pop("PRORES_SLOT_HELD", None)
        if slot is not None:
            slot.release()

    def execute_job(self, job, queue):  # type: ignore[override]
        try:
            return super().execute_job(job, queue)
        finally:
            self._release_prores_slot()

    def handle_job_failure(  # type: ignore[override]
        self, job, queue, started_job_registry=None, exc_string="",
    ):
        if exc_string and "ProResDefer" in exc_string:
            self._requeue_deferred_prores(job, queue, started_job_registry)
            return None
        return super().handle_job_failure(
            job, queue, started_job_registry=started_job_registry, exc_string=exc_string,
        )

    def handle_exception(self, job, *exc_info):  # type: ignore[override]
        """A defer is a requeue, not a failed job. Skip RQ's error traceback."""
        exc = exc_info[1] if len(exc_info) > 1 else None
        if exc is not None and exc.__class__.__name__ == "ProResDefer":
            logger.info(
                "[PRORES] %s deferred until an encode slot is free",
                getattr(job, "id", "?"),
            )
            return None
        return super().handle_exception(job, *exc_info)

    def _requeue_deferred_prores(self, job, queue, started_job_registry) -> None:
        """Put an encode-ready job back at the head without a failure."""
        logger.info(
            "[PRORES] encode slot busy; requeueing %s at the front of %s",
            getattr(job, "id", "?"), getattr(queue, "name", "?"),
        )
        self._drop_intermediate_job(queue, job)
        try:
            if started_job_registry is None:
                started_job_registry = queue.started_job_registry
            started_job_registry.remove(job)
        except Exception:
            logger.warning("[PRORES] could not leave the started registry", exc_info=True)
        try:
            self.set_current_job_id(None)
        except Exception:
            pass
        try:
            job.ended_at = None
            job.exc_info = None
        except Exception:
            pass
        try:
            from prores_capacity import mark_portal_demand, portal_queue_name
            if getattr(queue, "name", None) == portal_queue_name():
                mark_portal_demand(getattr(job, "id", "") or "")
        except Exception:
            logger.warning("[PRORES] could not republish portal demand", exc_info=True)
        queue.enqueue_job(job, at_front=True)

    def _drop_intermediate_job(self, queue, job) -> None:
        """RQ 1.16 LMOVEs a single-queue pop into an intermediate list.

        ``prepare_job_execution`` only LREMs that list when the worker
        listens to exactly one queue. This worker listens to portal and
        prewarm, so a successful job stayed in the intermediate list and
        the registry cleanup later marked it failed.
        """
        job_id = getattr(job, "id", None)
        key = getattr(queue, "intermediate_queue_key", None)
        connection = getattr(self, "connection", None)
        if not job_id or not key or connection is None:
            return
        try:
            connection.lrem(key, 1, job_id)
        except Exception:
            logger.warning("[PRORES] could not leave the intermediate queue", exc_info=True)

    def prepare_job_execution(  # type: ignore[override]
        self, job, remove_from_intermediate_queue=False,
    ):
        """Reject incompatible payloads inside RQ's failure boundary.

        Gated ProRes workers dequeue one queue at a time, which is the
        LMOVE path. Always drop the intermediate entry, even though this
        process listens to two queues.
        """
        from prores_capacity import gate_enabled_for
        from queue_jobs import validate_rq_payload_metadata

        if gate_enabled_for(self.queue_names()):
            remove_from_intermediate_queue = True
        validate_rq_payload_metadata(getattr(job, "meta", None))
        return super().prepare_job_execution(job, remove_from_intermediate_queue)

    @property
    def dequeue_timeout(self) -> int:
        """Wake idle BLPOP often enough to refresh release metadata.

        RQ's native timeout is ``worker_ttl - 15`` (405 seconds by default),
        while the release/protocol key intentionally expires much sooner so a
        dead or rolled-back worker leaves the readiness snapshot promptly.
        Each dequeue timeout returns through RQ's maintenance loop, which calls
        ``heartbeat()`` and republishes the custom key without adding a thread
        to a process that later forks work horses.
        """
        native_timeout = super().dequeue_timeout
        release_interval = max(_release_heartbeat_ttl_seconds() // 3, 1)
        return min(native_timeout, release_interval)

    def heartbeat(self, *args, **kwargs):  # type: ignore[override]
        """Extend RQ liveness with release, queue and protocol metadata."""
        result = super().heartbeat(*args, **kwargs)
        self._publish_release_heartbeat()
        return result

    def _publish_release_heartbeat(self) -> None:
        from queue_jobs import (
            RQ_PAYLOAD_VERSION,
            RQ_SUPPORTED_PAYLOAD_VERSIONS,
        )

        # Import local y tolerante: el heartbeat es lo que le dice al health que
        # este worker está vivo. Si la huella no se puede calcular, se manda
        # vacía y el gate cae a comparar el SHA — nunca se cae el heartbeat.
        try:
            from observability import (
                backend_code_fingerprint, runtime_timing_config,
                runtime_timing_config_effective,
            )
            _fingerprint = backend_code_fingerprint()
            _timing_config = runtime_timing_config()
            _timing_config_effective = runtime_timing_config_effective()
        except Exception:
            _fingerprint = "unknown"
            _timing_config = None
            _timing_config_effective = None
        try:
            from queue_jobs import _transcription_quality_runtime_token
            _runtime_token = _transcription_quality_runtime_token()
        except Exception:
            _runtime_token = None

        ttl = _release_heartbeat_ttl_seconds()
        queues = [getattr(q, "name", str(q)) for q in getattr(self, "queues", [])]
        payload = {
            "worker": getattr(self, "name", "unknown"),
            "service": os.environ.get("RAILWAY_SERVICE_NAME", "worker"),
            "release": (
                os.environ.get("RAILWAY_GIT_COMMIT_SHA")
                or os.environ.get("SENTRY_RELEASE")
                or os.environ.get("GIT_SHA")
                or "unknown"
            ),
            "queues": queues,
            # Huella del código de backend que este worker tiene cargado. Es lo
            # que el gate de flota compara de verdad: el SHA de git no sirve
            # porque los workers tienen filtros de path y saltean (bien) los
            # commits de sólo-frontend, lo que dejaba /health en `down` con todo
            # funcionando.
            "code_fingerprint": _fingerprint,
            # Token de identidad de runtime (política + release + las 72 flags
            # de configuración + calibración). Si difiere entre el servicio que
            # encola y el quality-worker, cada replay se descarta con
            # runtime_identity_mismatch sin dejar rastro; publicarlo acá es lo
            # que permite verlo en /health en vez de descubrirlo 21 días tarde.
            "runtime_token": _runtime_token,
            # API and every worker capable of transcription must agree on the
            # same timing constants. /health/ready compares this canonical
            # payload and fails closed on mismatch or missing publication.
            "timing_config": _timing_config,
            # Valor efectivo y origen (env / default / empty / invalid) de cada
            # variable de timing. Informativo: no entra en la paridad.
            "timing_config_effective": _timing_config_effective,
            "rq_payload_version": RQ_PAYLOAD_VERSION,
            "rq_supported_payload_versions": sorted(RQ_SUPPORTED_PAYLOAD_VERSIONS),
            "environment": (
                os.environ.get("RAILWAY_ENVIRONMENT_NAME")
                or os.environ.get("ENVIRONMENT", "production")
            ),
        }
        try:
            self.connection.setex(
                f"genly:worker:release:{payload['worker']}",
                ttl,
                json.dumps(payload, separators=(",", ":"), sort_keys=True),
            )
        except Exception as exc:  # native RQ heartbeat remains authoritative
            logger.debug("[WORKER] release heartbeat publish failed: %s", exc)


def _warn_if_shutdown_grace_too_short() -> None:
    """Loud startup warning when Railway's SIGTERM→SIGKILL grace is too
    short to let RQ drain an in-flight render. Without a 20-min grace,
    every redeploy of the Worker service kills any render mid-flight —
    operators see "El servidor se reinició mientras generábamos el
    video" and have to manually retry. See railway/worker.toml for the
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
    # 20 min = the value documented in railway/worker.toml for UMG-grade
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
            "Railway Config-as-Code.) See railway/worker.toml for context.",
            raw or "<unset>", REQUIRED_GRACE_S,
        )


_DEFAULT_QUEUES = "transcription,bg_preview,enterprise,default,audio_preview,canary"


def _resolve_queue_names() -> list:
    """Queue names this worker process listens on, in priority order.

    Env-driven (QUEUES, comma-separated) so the SAME image runs as a segmented
    fleet: a small ShortWorker pool (QUEUES=transcription,bg_preview,audio_preview)
    drains latency-sensitive and derivative jobs while the render pool
    (QUEUES=enterprise,default) owns heavy work. Editor previews stay on the
    short-worker pool so they can never occupy a render slot. RQ priority alone
    doesn't preempt — once all render workers are inside long renders, a short
    transcription waits the full render time; a dedicated pool fixes that.

    Default = the legacy shared queues = current single-pool behavior, so shipping this
    is a zero-behavior-change until QUEUES is set per Railway service.
    """
    raw = os.environ.get("QUEUES", _DEFAULT_QUEUES)
    names = [q.strip() for q in raw.split(",") if q.strip()]
    return names or _DEFAULT_QUEUES.split(",")


def _schedule_worker_maintenance(queue_names: list[str]) -> dict[str, bool]:
    """Seed only jobs this worker fleet can actually consume.

    Each scheduler is isolated so one unavailable subsystem cannot suppress
    the other recovery loops during startup.
    """
    scheduled: dict[str, bool] = {}
    if "transcription_quality" in queue_names:
        for name, import_name in (
            ("quality_learning", "ensure_daily_quality_learning_scheduled"),
            ("quality_pending", "ensure_quality_pending_reconciler_scheduled"),
        ):
            try:
                import queue_jobs
                getattr(queue_jobs, import_name)()
                scheduled[name] = True
            except Exception as exc:
                scheduled[name] = False
                logger.warning("[WORKER] %s scheduler unavailable: %s", name, exc)
        # Keep this new scheduler out of the legacy health result shape: older
        # admin callers compare the two established keys exactly. Its own RQ
        # id/audit row is the source of truth for research trigger status.
        try:
            import queue_jobs
            queue_jobs.ensure_learning_triggers_scheduled()
        except Exception as exc:
            logger.warning("[WORKER] learning trigger scheduler unavailable: %s", exc)
    if "default" in queue_names:
        try:
            from queue_jobs import ensure_job_outbox_reconciler_scheduled
            ensure_job_outbox_reconciler_scheduled()
            from queue_jobs import ensure_change_request_reconciler_scheduled
            ensure_change_request_reconciler_scheduled()
            scheduled["job_outbox"] = True
        except Exception as exc:
            scheduled["job_outbox"] = False
            logger.warning("[WORKER] job outbox scheduler unavailable: %s", exc)
    if "campaign_control" in queue_names:
        try:
            from batch_campaigns import ensure_campaign_reconciler_scheduled
            ensure_campaign_reconciler_scheduled()
            scheduled["batch_campaigns"] = True
        except Exception as exc:
            scheduled["batch_campaigns"] = False
            logger.warning("[WORKER] batch campaign scheduler unavailable: %s", exc)
    return scheduled


def _should_recycle(worker) -> bool:
    """True when worker.work() returned for a RECYCLABLE reason (the max_jobs
    cap, or a transient loop break like a Redis blip) rather than an
    operator/deploy WARM shutdown.

    Drives the os.execve self-recycle in main() (P0 2026-06-08).

    CRITICAL (adversarial review 2026-06-09): RQ 1.16.2's `_shutdown`
    (rq/worker.py:1000-1014) sets `_stop_requested = True` ONLY when the
    worker is BUSY at signal time; when it is IDLE (the dominant state for the
    ShortWorker — blocked in BLPOP between ~20s jobs) it `raise StopRequested`
    WITHOUT setting `_stop_requested`. So keying solely on `_stop_requested`
    misfired on the common case: a deploy's SIGTERM to an idle worker would
    look like a recycle and we'd os.execve (respawn old code, fight the
    rolling replace) instead of exiting. The reliable signal is
    `_shutdown_requested_date`, which `request_stop` (rq/worker.py:992) sets
    on EVERY warm shutdown, busy or idle. The max_jobs cap, Redis-timeout and
    unhandled-exception breaks set NEITHER flag → recycle. getattr defaults
    keep a future RQ rename from crashing the worker (it would recycle
    conservatively); test_recycle_discriminator_is_live pins the attributes."""
    return not (
        getattr(worker, "_stop_requested", False)
        or getattr(worker, "_shutdown_requested_date", None) is not None
    )


# Circuit breaker for the os.execve recycle (adversarial review 2026-06-09).
# execve keeps the SAME PID, so Railway's restartPolicyMaxRetries (ON_FAILURE)
# NEVER counts a re-exec — a worker that keeps returning with no work done
# (Redis outage, poison loop) would hot-loop forever. After N consecutive
# recycles that completed ZERO jobs we sys.exit(1) instead, ceding control to
# Railway's bounded ON_FAILURE restart. The streak survives execve via env.
_NOWORK_STREAK_ENV = "_WORKER_NOWORK_STREAK"
_MAX_NOWORK_RECYCLES = int(os.environ.get("WORKER_MAX_NOWORK_RECYCLES", "5"))


def _recycle_or_exit(worker, did_work: bool, max_jobs) -> None:
    """Decide what to do after worker.work() returns. Returns (caller should
    then `return` from main and exit the process) on a warm shutdown; on a
    recycle it re-execs a fresh process image (never returns) or, on a no-work
    hot-loop past the threshold, sys.exit(1) to cede to Railway's ON_FAILURE.

    `did_work` is worker.work()'s return value (bool(completed_jobs)): True ⇒
    at least one job ran this lifetime (a real max_jobs recycle); False ⇒ the
    loop broke without completing a job (Redis blip / unhandled exception)."""
    if not _should_recycle(worker):
        logger.info("[WORKER] warm shutdown requested — exiting for deploy/drain")
        return

    # No-work hot-loop guard. did_work resets the streak; a barren break grows it.
    streak = 0 if did_work else int(os.environ.get(_NOWORK_STREAK_ENV, "0")) + 1
    if streak >= _MAX_NOWORK_RECYCLES:
        logger.error(
            "[WORKER] %d recycles consecutivos sin completar trabajo — exit(1) "
            "para ceder a Railway ON_FAILURE (probable Redis caído / job-veneno)",
            streak,
        )
        sys.exit(1)

    backoff = float(os.environ.get("WORKER_RECYCLE_BACKOFF_S", "2"))
    if not did_work:  # exponential backoff on barren loops (cap 60s)
        backoff = min(backoff * (2 ** (streak - 1)), 60.0)
    logger.info(
        "[WORKER] recycle — re-exec'ing fresh worker (max_jobs=%s, nowork_streak=%d, backoff=%.0fs)",
        max_jobs, streak, backoff,
    )
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.flush()
        except Exception:
            pass
    time.sleep(backoff)
    # os.execve (not execv) so we can thread the streak counter across the
    # process-image replacement via the environment.
    os.execve(sys.executable, [sys.executable, *sys.argv],
              {**os.environ, _NOWORK_STREAK_ENV: str(streak)})


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
    from database import assert_quality_attempt_id_schema_contract
    assert_quality_attempt_id_schema_contract()
    _start_local_outputs_cleanup()

    from background_policy import (
        POLICY_ENV as _bg_policy_env,
        POLICY_VERSION as _bg_policy_version,
        VALID_POLICY_MODES as _bg_policy_modes,
        policy_mode as _bg_policy_mode,
    )
    from lyric_anchors import (
        ANCHORS_ENV as _lyric_anchors_env,
        VALID_ANCHOR_MODES as _lyric_anchor_modes,
        anchors_mode as _lyric_anchors_mode,
    )
    from observability import _resolve_release as _resolve_runtime_release
    logger.info(
        "[BG_POLICY][STARTUP] process=rq-worker release=%s environment=%s "
        "policy_version=%s policy_mode=%s lyric_anchor_mode=%s "
        "cache_namespace=%s queues=%s rq_payload_version=%s",
        _resolve_runtime_release(),
        os.environ.get("ENVIRONMENT", "production").lower().strip(),
        _bg_policy_version, _bg_policy_mode(), _lyric_anchors_mode(),
        _bg_policy_version,
        ",".join(_resolve_queue_names()),
        os.environ.get("RQ_PAYLOAD_VERSION", "2"),
    )
    _raw_bg_policy_mode = os.environ.get(_bg_policy_env, "off").strip().lower()
    if _raw_bg_policy_mode not in _bg_policy_modes:
        logger.warning(
            "[BG_POLICY][STARTUP] invalid %s=%r; resolved fail-safe to off",
            _bg_policy_env, _raw_bg_policy_mode,
        )
    _raw_lyric_anchor_mode = os.environ.get(_lyric_anchors_env, "off").strip().lower()
    if _raw_lyric_anchor_mode not in _lyric_anchor_modes:
        logger.warning(
            "[BG_POLICY][STARTUP] invalid %s=%r; resolved fail-safe to off",
            _lyric_anchors_env, _raw_lyric_anchor_mode,
        )

    _warn_if_shutdown_grace_too_short()

    from redis import Redis
    from rq import Queue  # the worker itself is WarmOnlyWorker (module-level)

    # Whisper preload is opt-in. The historical startup load pulled a 1.5 GB
    # model into every worker, including ProRes and render processes that
    # never transcribe. OPENAI_API_KEY still skips the local model.
    def _load_turbo():
        from pipeline import _get_whisper_model
        _get_whisper_model("turbo")

    _maybe_preload_whisper(_load_turbo)

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
    #   5. audio_preview — derivative-only AAC work, best effort and always
    #      behind generation/render work.
    # Workers listen in this order; RQ pickup respects it. The set is
    # env-driven (QUEUES) so this image can run as a segmented fleet — see
    # _resolve_queue_names(). Default = the shared legacy queues plus the
    # isolated audio-preview queue.
    queue_names = _resolve_queue_names()
    queues = [Queue(name, connection=conn) for name in queue_names]
    _schedule_worker_maintenance(queue_names)
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
    did_work = worker.work(with_scheduler=True, max_jobs=max_jobs)

    # Self-supervised recycle (P0 2026-06-08). worker.work() returns on three
    # paths: (a) the max_jobs cap above, (b) a transient break (Redis blip,
    # unhandled exception in the loop), or (c) a SIGTERM/SIGINT warm shutdown
    # (Railway deploy). The original code let main() return in ALL three,
    # ASSUMING — see the max_jobs comment above — that "Railway's restart
    # policy spawns a replacement in ~30s". It does NOT: every worker service
    # runs restartPolicyType=ON_FAILURE (railway/worker.toml), so a CLEAN exit (code
    # 0) is treated as success and is never restarted. After one recycle the
    # ShortWorker pool — the sole consumer of the `transcription` queue
    # (QUEUES=transcription,bg_preview) — exited and stayed dead;
    # transcriptions sat in `transcribing_queued` with no consumer until the
    # reaper flipped them to "Worker colgado" (universal_argentina,
    # 2026-06-08). The render Worker pool masked the same latent bug via 7
    # replicas + frequent deploys.
    #
    # Fix: be our own supervisor. On a warm shutdown (case c) exit so the
    # rolling deploy proceeds and WarmOnlyWorker's drain is honored; on a
    # recycle (a/b) re-exec a fresh process image to keep the queue consumer
    # alive without depending on Railway's restart policy. The warm-shutdown
    # vs recycle discrimination and the no-work hot-loop guard live in
    # _recycle_or_exit / _should_recycle (see their docstrings). NB: the
    # leaked memory that motivates max_jobs lives in RQ's per-job forked
    # work-horse, which already dies per job — the recycle's real value is
    # keeping the consumer ALIVE, not heap reset.
    _recycle_or_exit(worker, did_work, max_jobs)


if __name__ == "__main__":
    main()
