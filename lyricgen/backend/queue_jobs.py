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
import hashlib
import threading

logger = logging.getLogger("genly.queue")

REDIS_URL = os.environ.get("REDIS_URL", "").strip()

# Versioned metadata for every RQ enqueue. Keeping the version in Job.meta
# (instead of changing the serialized function signature) makes rolling
# deploys safe in both directions: old workers ignore the new metadata and
# new workers treat metadata-less jobs already in Redis as v1.
RQ_PAYLOAD_VERSION = int(os.environ.get("RQ_PAYLOAD_VERSION", "2"))
if RQ_PAYLOAD_VERSION < 2:
    raise RuntimeError("RQ_PAYLOAD_VERSION must be >= 2")
RQ_SUPPORTED_PAYLOAD_VERSIONS = frozenset(
    (RQ_PAYLOAD_VERSION - 1, RQ_PAYLOAD_VERSION)
)


class UnsupportedRQPayloadVersion(RuntimeError):
    """Raised before work starts when API and worker protocols cannot agree."""


class SubmissionsPausedError(RuntimeError):
    """Raised when an internal enqueue bypasses the HTTP maintenance gate."""


def _require_submissions_open() -> None:
    from ops_control import get_submissions_state
    state = get_submissions_state()
    if state.get("paused"):
        raise SubmissionsPausedError(
            state.get("reason") or "new submissions are temporarily paused"
        )


def rq_payload_metadata(kind: str, **extra) -> dict:
    """Metadata attached to new jobs without altering their call signature."""
    return {
        "rq_payload_version": RQ_PAYLOAD_VERSION,
        "rq_payload_kind": kind,
        "producer_release": (
            os.environ.get("RAILWAY_GIT_COMMIT_SHA")
            or os.environ.get("SENTRY_RELEASE")
            or os.environ.get("GIT_SHA")
            or "unknown"
        ),
        **extra,
    }


def validate_rq_payload_metadata(meta: dict | None) -> int:
    """Normalize the protocol version and reject incompatible jobs.

    Jobs created before this protocol had no metadata and are explicitly v1.
    Only N and N-1 are accepted so a mixed fleet cannot silently execute
    payload semantics it does not understand.
    """
    raw = (meta or {}).get("rq_payload_version", 1)
    if isinstance(raw, bool):
        from ops_metrics import increment
        increment("rq_payload_incompatible")
        raise UnsupportedRQPayloadVersion("boolean RQ payload version is invalid")
    try:
        version = int(raw)
    except (TypeError, ValueError) as exc:
        from ops_metrics import increment
        increment("rq_payload_incompatible")
        raise UnsupportedRQPayloadVersion(
            f"invalid RQ payload version: {raw!r}"
        ) from exc
    if version not in RQ_SUPPORTED_PAYLOAD_VERSIONS:
        from ops_metrics import increment
        increment("rq_payload_incompatible")
        supported = ",".join(str(v) for v in sorted(RQ_SUPPORTED_PAYLOAD_VERSIONS))
        raise UnsupportedRQPayloadVersion(
            f"RQ payload v{version} is incompatible; worker accepts {supported}"
        )
    return version

JOB_TIMEOUT = int(os.environ.get("JOB_TIMEOUT_SECONDS", "2700"))  # 45 min (YouTube)
# RQ auto-retry on worker death (Railway redeploy, OOM, hard signal).
# RQ moves orphaned jobs from StartedJobRegistry to FailedJobRegistry
# via cleanup_ghosts() on the next worker boot, raising AbandonedJobError.
# Retry handles that path.
#
# Bumped from 1 → 3 on 2026-05-19 after the agus.cafisi Una Vez Más
# incident where two back-to-back PR merges to staging killed the same
# render twice — max=1 means a SINGLE deploy during the retry window
# turns the job permanent-failed. With max=3 the job survives up to
# three consecutive redeploys before the failure callback gives up and
# surfaces the user-facing "Reintentar" button. The real fix is setting
# RAILWAY_SHUTDOWN_TIMEOUT_SECONDS=1200 on the Worker service (per
# railway/worker.toml note) so SIGTERM has 20 min to drain in-flight renders,
# but the retry bump is the cheap belt-and-suspenders for cases where
# SIGKILL still hits (very long render, or env var forgotten on a new
# environment).
PIPELINE_RETRY_MAX = int(os.environ.get("PIPELINE_RETRY_MAX", "3"))
# Backoff between attempts. 60 s gives the new worker pod time to come
# up after a Railway redeploy AND for any quick second redeploy to
# settle before we re-claim — 30 s was just barely enough for one
# deploy, and got hit by the second deploy of a tight PR-merge window.
PIPELINE_RETRY_INTERVAL_S = int(os.environ.get("PIPELINE_RETRY_INTERVAL_S", "60"))
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


def transcription_quality_queue_enabled() -> bool:
    return os.environ.get(
        "TRANSCRIPTION_QUALITY_QUEUE_ENABLED", "0"
    ).strip().lower() in {"1", "true", "yes", "on"}


def quality_learning_capture_enabled() -> bool:
    return (
        transcription_quality_queue_enabled()
        and os.environ.get("QUALITY_LEARNING_CAPTURE_ENABLED", "0").strip().lower()
        in {"1", "true", "yes", "on"}
    )


def transcription_quality_rollout_eligible(
    job_id: str, tenant_id: str | None = None,
) -> bool:
    """Stable pilot/percentage allocation; never random across retries."""
    if not transcription_quality_queue_enabled():
        return False
    pilots = {
        value.strip() for value in os.environ.get(
            "TRANSCRIPTION_QUALITY_PILOT_TENANTS", "",
        ).split(",") if value.strip()
    }
    if tenant_id and str(tenant_id) in pilots:
        return True
    try:
        percentage = float(os.environ.get(
            "TRANSCRIPTION_QUALITY_ROLLOUT_PERCENT", "0",
        ))
    except (TypeError, ValueError):
        percentage = 0.0
    percentage = max(0.0, min(100.0, percentage))
    bucket = int(hashlib.sha256(str(job_id).encode("utf-8")).hexdigest()[:8], 16)
    return bucket % 10_000 < round(percentage * 100)

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
        # Fix U11-hotpath: añadir timeouts para que un Redis lento o
        # inaccesible no bloquee el proceso en el primer connect/ping.
        _redis = Redis.from_url(REDIS_URL, socket_connect_timeout=2, socket_timeout=5)
        _redis.ping()
        _queue_default = Queue("default", connection=_redis)
        _queue_enterprise = Queue("enterprise", connection=_redis)
        return _redis, _queue_default, _queue_enterprise
    except Exception as e:
        logger.warning("[QUEUE] Redis init failed (%s); falling back to threads", e)
        return None, None, None


# Tenant IDs that always route to the enterprise queue regardless of
# the operator's stated `plan`. These are the B2B customers whose
# batches are time-sensitive (UMG release schedules, OMG delivery
# windows) — they should never queue behind a free-tier user.
#
# Override via env (`ENTERPRISE_TENANTS=umg,omg,acme`). Comma-separated,
# case-insensitive. Default covers the two tenants paying right now.
#
# Why this lives next to _pick_queue and not in a tenants.py file: it
# changes the routing of every enqueue path (generate, edit, prewarm,
# variant). Co-locating the policy with the function that consumes it
# makes the precedence rules visible at the call site.
_ENTERPRISE_TENANTS = frozenset(
    t.strip().lower()
    for t in os.environ.get("ENTERPRISE_TENANTS", "umg,omg").split(",")
    if t.strip()
)

# Synthetic-traffic tenants (monitors/canaries) drain LAST. The golden
# render bot fires 3 concurrent renders on every staging push + nightly
# cron; on 2026-07-17 that tripled the default-queue wait for a real
# operator mid-test. Canary jobs must never compete with humans — they
# only need to run *eventually* to keep the regression signal alive.
# The worker must list "canary" at the END of its QUEUES env or these
# jobs will sit unconsumed (see worker.py:_resolve_queue_names).
_CANARY_TENANTS = frozenset(
    t.strip().lower()
    for t in os.environ.get("CANARY_TENANTS", "golden_render_bot").split(",")
    if t.strip()
)


def _pick_queue(plan: str, tenant_id: str = ""):
    """Enterprise queue for premium plans OR B2B tenants, default otherwise.

    Precedence (highest first):
      0. tenant_id matches `_CANARY_TENANTS` — synthetic monitors drain
         last, whatever their plan says (a canary must never gain fast
         lane by a plan change).
      1. tenant_id matches `_ENTERPRISE_TENANTS` — UMG/OMG always jump
         the default queue even on plan="100". 2026-05-15 incident:
         agus.cafisi (omg) batch of 5 songs was queuing behind tomas's
         single-user work because both were on default queue.
      2. plan == "unlimited" or "enterprise" — legacy SaaS path; kept
         so a non-B2B tenant on an "enterprise" plan still gets fast
         lane.
      3. Everything else lands on default.

    Workers listen to enterprise first then default, so an enterprise
    job always pre-empts a default job at the worker-pickup point.
    Higher tenant priority within a single queue is RQ's natural FIFO,
    so a fresh enterprise enqueue still waits for the current
    enterprise job to finish — but never for a default-queue job.
    """
    _, q_default, q_enterprise = _init_redis()
    if q_default is None:
        return None
    tid = (tenant_id or "").strip().lower()
    if tid and tid in _CANARY_TENANTS:
        from rq import Queue
        return Queue("canary", connection=q_default.connection)
    if tid and tid in _ENTERPRISE_TENANTS:
        return q_enterprise
    if plan in ("unlimited", "enterprise"):
        return q_enterprise
    return q_default


def _capture_job_failure(layer: str, job_id_db: str, type_, value) -> None:
    """Send a tagged Sentry event for a permanently-failed RQ job.

    Failure callbacks are the chokepoint where EVERY exhausted job lands
    (worker death, OOM, real pipeline exceptions), so this is the one
    place that guarantees a job failure reaches Sentry tagged with our
    Postgres job_id + tenant_id — RqIntegration tags by RQ job id only,
    which is not what operators search by during an incident.

    Best-effort by contract: any exception here is swallowed so the
    callback's real work (flipping the DB row to a terminal error state)
    is never compromised by observability.
    """
    try:
        import sentry_sdk
        with sentry_sdk.push_scope() as scope:
            scope.set_tag("event", f"{layer}.job_failed")
            scope.set_tag("layer", layer)
            scope.set_tag("job_id", job_id_db or "?")
            # tenant_id best-effort: a failed UMG job and a failed free-tier
            # job have very different incident severity.
            try:
                from database import SessionLocal, Job
                _s = SessionLocal()
                try:
                    row = _s.query(Job).filter(Job.job_id == job_id_db).first()
                    if row is not None and row.tenant_id:
                        scope.set_tag("tenant_id", row.tenant_id)
                finally:
                    _s.close()
            except Exception:
                pass
            if value is not None and isinstance(value, BaseException):
                sentry_sdk.capture_exception(value)
            else:
                sentry_sdk.capture_message(
                    f"[{layer}] job {job_id_db} failed: "
                    f"{type_.__name__ if type_ else 'unknown'}",
                    level="error",
                )
    except Exception:  # pragma: no cover
        pass


def transcription_failure_callback(job, connection, type_, value, traceback) -> None:
    """RQ on_failure hook for `run_transcription_job`. Mirrors
    `pipeline_failure_callback` but writes the transcription-specific
    terminal status.

    INCIDENT 2026-05-24: when RQ killed the work-horse (timeout exceeded,
    OOM, deploy SIGTERM), the Postgres row stayed in `status='transcribing'`
    indefinitely because `transcription_worker._fail` runs INSIDE the
    Python process — a SIGKILL skips it entirely. Operator saw 4 jobs
    "stuck at isolate_vocals progress=25" with no error. RQ does call its
    own `failure_callbacks` AFTER the kill, so this hook is the last
    chance to surface a real error to the user.

    Audit 2026-05-26: routes through update_job (same reasoning as
    pipeline_failure_callback). Note `transcription_failed` is currently
    NOT in update_job's terminal set (_TERMINAL_STATUSES), so the
    terminal-state guard treats this as a regular update — which is
    fine because the only safer states than transcription_failed are
    the truly-terminal ones (done, rejected, etc.) and update_job won't
    flip from any of those backward thanks to its own guard.
    """
    try:
        from jobs import update_job
        rq_job_id = getattr(job, "id", "") or ""
        # RQ job_id has prefix `transcribe:<job_id>` (set at enqueue time).
        if rq_job_id.startswith("transcribe:"):
            job_id_db = rq_job_id.split(":", 1)[1]
        else:
            job_id_db = rq_job_id
        if not job_id_db:
            return
        # Surface to Sentry BEFORE touching the DB — this hook also covers
        # the SIGKILL case where transcription_worker's in-process capture
        # never got the chance to run.
        _capture_job_failure("transcription", job_id_db, type_, value)
        is_abandoned = "AbandonedJobError" in (type_.__name__ if type_ else "")
        if is_abandoned:
            err_msg = (
                "El worker se reinició mientras transcribíamos y los "
                "reintentos automáticos también fallaron. Reintentá "
                "subiendo el archivo de nuevo."
            )
        else:
            tb_msg = str(value)[:400] if value else (type_.__name__ if type_ else "error")
            err_msg = f"La transcripción falló: {tb_msg}"
        update_job(
            job_id_db,
            status="transcription_failed",
            error=err_msg[:500],
            current_step="error",
        )
    except Exception as e:  # pragma: no cover
        logger.warning("transcription_failure_callback failed: %s", e)


def pipeline_failure_callback(job, connection, type_, value, traceback) -> None:
    """RQ on_failure hook for run_pipeline. Fires when retries are
    exhausted (i.e. the job is permanently dead). Updates the Postgres
    row so the user sees a clear "Reintentar sin re-subir" affordance
    instead of a frozen "Generando" or a generic infra error.

    The signature matches RQ's failure-callback contract: (job, connection,
    type_, value, traceback). type_/value identify the failure class —
    AbandonedJobError means a worker died mid-render (deploy/OOM/SIGKILL),
    everything else is a real exception inside the pipeline.

    Audit 2026-05-26 (systemic-jobs-pipeline): routes through
    jobs.update_job() instead of a raw SELECT + UPDATE. update_job has
    the FOR UPDATE row lock that prevents the same race the reaper had
    (callback reads status=processing, worker concurrently flips to
    status=done, callback overwrites with error → user loses a video
    that actually completed). update_job's terminal-state guard also
    means we no longer need the inline `if row.status in ("processing",
    "queued")` check — passing status="error" is a terminal target,
    which always lands, but it lands ATOMICALLY behind the row lock,
    so the worker's "done" wins if it commits first.

    Best-effort: any exception in here is swallowed so RQ's own failure
    bookkeeping still completes — a noisy callback that breaks the
    failure path is worse than no callback at all.
    """
    try:
        from jobs import update_job
        # RQ's job.id == our job_id (we map them 1:1 in enqueue_pipeline).
        rq_job_id = getattr(job, "id", None) or ""
        if not rq_job_id:
            return
        # Surface to Sentry tagged with job/tenant — a permanently-dead
        # render is always incident-worthy, doubly so for a B2B tenant.
        _capture_job_failure("render_pipeline", rq_job_id, type_, value)
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
        # update_job's terminal-state guard means status="error" lands
        # even if the row is currently "processing"/"queued" (target is
        # terminal → guard always lets it through), but loses cleanly
        # to a concurrent "done" because both contend on the same
        # FOR UPDATE lock.
        update_job(rq_job_id, status="error", error=err_msg[:500])
    except Exception as e:  # pragma: no cover
        logger.warning("pipeline_failure_callback failed: %s", e)


def cancel_rq_job(job_id: str) -> bool:
    """Delete a job from RQ entirely. Returns True if a job was removed.

    Closes the reaper-vs-RQ desync where the reaper marks a row as
    `error` in Postgres but the RQ entry stays alive — on the next
    worker restart, RQ's Retry / cleanup_ghosts path resurrects the
    job and the worker burns 20 min re-processing a row that is
    already terminal. The worker's pipeline-end update is then
    refused by jobs.update_job's terminal-state guard, silently
    discarding the result.

    Strategy: remove from every registry that could re-enqueue it.
    - StartedJobRegistry: jobs claimed by a worker that died.
    - FailedJobRegistry: jobs RQ already marked failed; deleting
      prevents an operator-triggered RQ requeue from picking them up.
    - DeferredJobRegistry / ScheduledJobRegistry: jobs waiting on a
      retry timer (PIPELINE_RETRY_INTERVAL_S backoff).
    - The queue itself: pending jobs not yet picked.
    - Job.delete(): the canonical RQ hash + dependency links.

    Best-effort: any failure is logged and swallowed. Reaper still
    completes its DB updates — RQ leftovers are a recoverable mess,
    a crashing reaper is not.
    """
    if not job_id:
        return False
    r, q_default, q_enterprise = _init_redis()
    if r is None:
        return False
    try:
        from rq.job import Job as RqJob
        from rq.exceptions import NoSuchJobError
    except Exception as e:  # pragma: no cover
        logger.warning("RQ import failed in cancel_rq_job: %s", e)
        return False
    try:
        rq_job = RqJob.fetch(job_id, connection=r)
    except NoSuchJobError:
        return False
    except Exception as e:  # pragma: no cover
        logger.warning("RQ fetch failed for %s: %s", job_id, e)
        return False
    try:
        from rq import Queue as _Q
        from rq.registry import (
            StartedJobRegistry, FailedJobRegistry,
            DeferredJobRegistry, ScheduledJobRegistry,
        )
        # Build the full set of queues a job could be in. _init_redis only
        # exposes default + enterprise (legacy), but transcription jobs use
        # the `transcription` queue (queue_jobs.py:435) and bg_preview jobs
        # use `bg_preview` (queue_jobs.py:514). Before this fix, the reaper
        # would mark a transcription row as transcription_failed in Postgres
        # but the RQ entry stayed alive in the `transcription` queue's
        # ScheduledJobRegistry — RQScheduler would later move it back to
        # `transcription`, the worker would re-process a row already in a
        # terminal state, and jobs.update_job's terminal-state guard would
        # silently discard the result (incident 2026-05-26).
        all_queues = [q_default, q_enterprise]
        for extra_name in ("transcription", "transcription_quality", "bg_preview"):
            try:
                all_queues.append(_Q(extra_name, connection=r))
            except Exception:
                pass
        for q in all_queues:
            if q is None:
                continue
            try:
                q.remove(job_id)
            except Exception:
                pass
            for reg_cls in (StartedJobRegistry, FailedJobRegistry,
                            DeferredJobRegistry, ScheduledJobRegistry):
                try:
                    reg_cls(queue=q).remove(job_id, delete_job=False)
                except Exception:
                    pass
    except Exception as e:  # pragma: no cover
        logger.warning("RQ registry cleanup failed for %s: %s", job_id, e)
    try:
        rq_job.delete()
    except Exception as e:  # pragma: no cover
        logger.warning("RQ Job.delete failed for %s: %s", job_id, e)
    return True


def _evict_stale_rq_job(connection, rq_job_id: str) -> None:
    """Delete any RQ Job with `rq_job_id` from Redis before re-enqueueing.

    Audit 2026-05-26 (jobs-pipeline-systemic-audit). Without this, calling
    `q.enqueue(..., job_id=X)` for an X that already exists in Redis (from
    a previous failed/completed run) silently DEDUPES — RQ returns the
    cached Job, ignores the new args/kwargs, and the worker either does
    nothing (failed jobs sit in FailedJobRegistry forever) or re-runs the
    OLD args (defeating /retry, frame_size override, preserved_bg_r2_key,
    bypass_content_validation, etc.).

    `enqueue_edit` already had this logic inline since the original UMG
    edit-resurrection incident. Extracted here so the three queue-using
    helpers stay consistent. Best-effort; a Redis hiccup during the fetch
    must not block the enqueue.
    """
    try:
        from rq.job import Job as RQJob
        stale = RQJob.fetch(rq_job_id, connection=connection)
        stale.delete()
    except Exception:
        pass  # no stale job, or Redis hiccup — proceed normally


def _active_rq_job(connection, rq_job_id: str):
    """Return an existing live job; terminal/missing entries return None."""
    try:
        from rq.job import Job as RQJob
        existing = RQJob.fetch(rq_job_id, connection=connection)
        status = existing.get_status(refresh=True)
        value = str(getattr(status, "value", status) or "").lower()
        if value in {"queued", "started", "deferred", "scheduled"}:
            return existing
    except Exception:
        return None
    return None


def enqueue_pipeline(
    job_id: str,
    mp3_path: str,
    artist: str,
    style: str,
    plan: str = "100",
    tenant_id: str = "",
    **kwargs,
) -> str:
    """Enqueue a run_pipeline job. Returns RQ job id (or 'thread:<job_id>' in
    the Redis-less fallback path)."""
    _require_submissions_open()
    # Internal lockstep token: a worker running a different rollout mode must
    # fail before generation rather than silently producing under another
    # policy. This is RQ metadata, not a public request/payload field.
    from background_policy import runtime_rollout_fingerprint
    kwargs = dict(kwargs)
    policy_fingerprint = runtime_rollout_fingerprint()
    # Preserve the origin/staging invocation payload so a v1 worker can
    # execute a v2-produced job during the bounded cutover. Metadata is the
    # new source for v2 workers; the legacy argument remains for N-1.
    kwargs["background_policy_fingerprint"] = policy_fingerprint
    q = _pick_queue(plan, tenant_id=tenant_id)
    if q is not None:
        from rq import Retry
        from pipeline import run_pipeline
        # Evict any stale RQ entry with the same job_id. /retry re-uses the
        # same job_id for the same Postgres row; without this evict, the
        # second enqueue would silently reuse the failed first attempt's
        # cached args (no preserved_bg_r2_key, no frame_size override, etc.)
        # and the operator would see "Retry" do exactly nothing useful.
        # See _evict_stale_rq_job docstring for the full reasoning.
        _evict_stale_rq_job(q.connection, job_id)
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
            meta=rq_payload_metadata(
                "pipeline", background_policy_fingerprint=policy_fingerprint,
            ),
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


def enqueue_transcription(
    job_id: str,
    audio_path: str,
    *,
    language: str = "",
    artist: str = "",
    title: str = "",
    filename: str = "",
    live: bool = False,
    tenant_id: str = "",
    anchor_lyrics: str = "",
) -> str:
    """Enqueue una transcripción en la queue `transcription` (alta prioridad,
    drenada por el mismo worker container que enterprise/default).

    Devuelve el RQ job id (o 'thread:<job_id>' en el fallback dev sin Redis).

    Diseño 2026-05-23: antes `/transcribe-uploaded` corría
    `_run_transcription_for_job` inline. Ahora hace enqueue acá y devuelve
    202+job_id. Ver transcription_worker.py para el entry point + el code path.

    Tenant priority: UMG/OMG aterrizan en `transcription` igual que todos —
    Whisper es uniformemente rápido (~15-20s), no necesita una cola premium
    aparte. Si en el futuro la cola se acumula y un tenant grande está
    bloqueado, mover la decisión de queue acá.
    """
    # Tenants de un solo idioma (UMG Chile = siempre español) fijan el idioma
    # acá para que WhisperX no lo auto-detecte mal y envenene la cache de
    # transcripción con `en` (incidente 2026-08-12, Sebastián/UMG Chile).
    # Aplica al path real del frontend (enqueue → ShortWorker).
    from transcription_language import forced_language_for_tenant
    language = forced_language_for_tenant(tenant_id, language)

    _require_submissions_open()
    _, q_default, _ = _init_redis()
    if q_default is not None:
        # Acceso directo al Redis para crear la queue "transcription" sin
        # cambiar la inicialización (que no la incluye por compat con workers
        # existentes que no la conocen).
        from rq import Queue, Retry
        q = Queue("transcription", connection=_redis)
        from transcription_worker import run_transcription_job
        # INCIDENT 2026-05-24: previous default was 300s (5 min). The
        # post-PR-G pipeline runs demucs (60-180s) + forced_align (75-480s
        # budget) + whisperX (60-480s budget) + Whisper-1 fallback. Worst
        # case ~15 min — RQ killed the work-horse at 5 min, the job stayed
        # in `status='transcribing'` indefinitely (no finally hook on the
        # kill signal), and the operator saw "stuck at progress=25" with
        # no error. Bump default to 1800s (30 min) — covers the worst
        # case with margin. Env var override stays for ops tuning.
        timeout = int(os.environ.get("TRANSCRIBE_JOB_TIMEOUT", "1800"))
        retry = Retry(max=2, interval=10)  # whisper hiccup → reintentar 2 veces con 10s gap
        # Evict stale RQ entry from a previous attempt — same reasoning as
        # enqueue_pipeline. Without this, a transcription retry would silently
        # re-use the failed first job and the operator's edit (filename,
        # artist override, etc.) would be ignored. Audit 2026-05-26.
        _evict_stale_rq_job(_redis, f"transcribe:{job_id}")
        rq_job = q.enqueue(
            run_transcription_job,
            args=(job_id, audio_path),
            kwargs={
                "language": language, "artist": artist, "title": title,
                "filename": filename, "live": live,
                "anchor_lyrics": anchor_lyrics,
            },
            job_timeout=timeout,
            result_ttl=RESULT_TTL,
            failure_ttl=FAILURE_TTL,
            job_id=f"transcribe:{job_id}",  # prefix evita colisión con render job_id
            meta=rq_payload_metadata("transcription"),
            retry=retry,
            # INCIDENT 2026-05-24: when RQ killed the work-horse (timeout
            # or OOM), `transcription_worker._fail` never ran (it lives
            # inside the Python process). Without this callback the
            # Postgres row stayed in `transcribing` indefinitely. Now
            # RQ calls this AFTER the kill to mark the job
            # `transcription_failed` so the operator sees a real error.
            on_failure=transcription_failure_callback,
        )
        return rq_job.id

    # Dev fallback (sin Redis): thread daemon, idéntico al de enqueue_pipeline.
    if _ENVIRONMENT == "production":
        logger.error(
            "Refusing to enqueue transcription %s via thread fallback: Redis is "
            "required in production but unreachable.", job_id,
        )
        raise RuntimeError(
            "Transcription queue unavailable: Redis is required in production."
        )
    from transcription_worker import run_transcription_job
    t = threading.Thread(
        target=run_transcription_job,
        args=(job_id, audio_path),
        kwargs={
            "language": language, "artist": artist, "title": title,
            "filename": filename, "live": live,
            "anchor_lyrics": anchor_lyrics,
        },
        daemon=True,
    )
    t.start()
    return f"thread:transcribe:{job_id}"


def enqueue_bg_preview(
    job_id: str,
    bg_cache_key: str,
    params: dict,
) -> str:
    """Enqueue una pre-generación del background a la queue `bg_preview`.

    Capa C del wizard refactor (2026-05-24): mientras el operador edita lyrics,
    Veo/Imagen ya están generando el fondo. Cuando llega el POST /generate
    "real", el video del background ya está cacheado en R2 y la pipeline lo
    reusa — 0 cost extra y ~60-120s menos de wait perceptible.

    Devuelve el RQ job id (o 'thread:bgpreview:<job_id>' en dev fallback sin
    Redis). Ver bg_preview.py para el entry point del worker.

    Tenant priority: por ahora todos en `bg_preview` queue (workers drenan
    en orden FIFO). Si en el futuro UMG necesita priority, mover a
    `enterprise_bg_preview` o similar.
    """
    _require_submissions_open()
    from background_policy import runtime_rollout_fingerprint
    _policy_fingerprint = runtime_rollout_fingerprint()
    _, q_default, _ = _init_redis()
    if q_default is not None:
        from rq import Queue, Retry
        q = Queue("bg_preview", connection=_redis)
        from bg_preview import run_bg_preview_job
        # job_timeout DEBE superar el poll_deadline de Veo (600s en
        # pipeline._generate_veo_video) o RQ mata el worker a mitad de vuelo
        # con JobTimeoutException antes de que Veo termine. 900s cubre el caso
        # típico (Veo poll 60-180s + 1 do-over: retry transitorio o re-roll por
        # calidad) + upload a R2. NO cubre el peor-caso de cola (429-storm +
        # dos polls de 600s completos ≈ 1200s+): ahí el death-penalty igual
        # dispara, pero ahora degrada a gradient limpio + lo agarra el monitor.
        # Si ese tail aparece en prod, subir BG_PREVIEW_JOB_TIMEOUT por env
        # hacia ~1100s (bajo el grace de 1200s de Railway) sin tocar código.
        # El loop interno de Veo hace 1 retry (2 intentos, antes 3). Nota: las
        # fallas de Veo caen al gradient fallback (no re-lanzan), así que el RQ
        # Retry(max=2) de abajo NO reintenta Veo — sólo fallos de R2/infra que
        # sí propagan. (Sentry "Veo 3 JobTimeoutException 300s")
        timeout = int(os.environ.get("BG_PREVIEW_JOB_TIMEOUT", "900"))
        # Veo es lento + hiccup-prone; 2 retries con 20s gap absorbe la
        # mayoría de los rate-limits transitorios. Más allá de eso, el job
        # marca status=bg_preview_failed y el frontend muestra el error.
        retry = Retry(max=2, interval=20)
        # Evict stale RQ entry — same reasoning as enqueue_pipeline.
        # Audit 2026-05-26.
        _evict_stale_rq_job(_redis, f"bgpreview:{job_id}")
        rq_job = q.enqueue(
            run_bg_preview_job,
            args=(job_id, bg_cache_key, params, _policy_fingerprint),
            job_timeout=timeout,
            result_ttl=RESULT_TTL,
            failure_ttl=FAILURE_TTL,
            job_id=f"bgpreview:{job_id}",
            meta=rq_payload_metadata(
                "bg_preview", background_policy_fingerprint=_policy_fingerprint,
            ),
            retry=retry,
        )
        return rq_job.id

    if _ENVIRONMENT == "production":
        logger.error(
            "Refusing to enqueue bg_preview %s via thread fallback: Redis is "
            "required in production but unreachable.", job_id,
        )
        raise RuntimeError(
            "bg_preview queue unavailable: Redis is required in production."
        )
    from bg_preview import run_bg_preview_job
    t = threading.Thread(
        target=run_bg_preview_job,
        args=(job_id, bg_cache_key, params, _policy_fingerprint),
        daemon=True,
    )
    t.start()
    return f"thread:bgpreview:{job_id}"


def enqueue_transcription_quality(job_id: str, *, expected_revision: int,
                                  expected_segments_hash: str,
                                  filename: str = "",
                                  tenant_id: str | None = None) -> str:
    """Queue suggestion-only quality analysis on its isolated worker.

    Failure to enqueue never falls back to an API/short-worker thread: doing
    so would defeat the latency isolation this queue exists to provide.
    """
    if not transcription_quality_queue_enabled():
        try:
            from ops_metrics import increment
            increment("transcription_quality_queue_enqueue_disabled")
        except Exception:
            pass
        return f"disabled:transcription-quality:{job_id}"
    if not transcription_quality_rollout_eligible(job_id, tenant_id):
        try:
            from ops_metrics import increment
            increment("transcription_quality_queue_rollout_excluded")
        except Exception:
            pass
        return f"rollout-excluded:transcription-quality:{job_id}"
    _require_submissions_open()
    _init_redis()
    if _redis is None:
        raise RuntimeError("transcription quality queue unavailable")
    from rq import Queue, Retry
    from quality_jobs import (
        run_transcription_quality_job,
        transcription_quality_failure_callback,
    )

    queue = Queue("transcription_quality", connection=_redis)
    rq_id = (
        f"transcription-quality:{job_id}:{int(expected_revision)}:"
        f"{expected_segments_hash[:12]}"
    )
    active = _active_rq_job(_redis, rq_id)
    if active is not None:
        try:
            from ops_metrics import increment
            increment("transcription_quality_queue_deduplicated")
        except Exception:
            pass
        return active.id
    _evict_stale_rq_job(_redis, rq_id)
    queued = queue.enqueue(
        run_transcription_quality_job,
        args=(job_id,),
        kwargs={
            "expected_revision": int(expected_revision),
            "expected_segments_hash": expected_segments_hash,
            "filename": filename,
        },
        job_timeout=int(os.environ.get("TRANSCRIPTION_QUALITY_JOB_TIMEOUT", "1200")),
        result_ttl=RESULT_TTL, failure_ttl=FAILURE_TTL,
        job_id=rq_id,
        meta=rq_payload_metadata(
            "transcription_quality", expected_revision=int(expected_revision),
            expected_segments_hash=expected_segments_hash,
        ),
        retry=Retry(max=1, interval=30),
        on_failure=transcription_quality_failure_callback,
    )
    try:
        from ops_metrics import increment
        increment("transcription_quality_queue_enqueued")
    except Exception:
        pass
    return queued.id


def enqueue_correction_learning(job_id: str, approved_version_id: str, *,
                                active_edit_ms: int | None = None,
                                source_confidence: str = "exact",
                                session_id: str | None = None) -> str:
    """Capture one approved correction on the isolated quality queue.

    This never falls back to an API thread and never participates in the
    percentage rollout: capture has its own explicit kill switch.
    """
    if not quality_learning_capture_enabled():
        return f"disabled:correction-learning:{job_id}"
    _init_redis()
    if _redis is None:
        raise RuntimeError("correction learning queue unavailable")
    from rq import Queue, Retry
    from correction_learning import derive_server_active_edit_ms, hmac_identifier
    from database import EditorDocument, EditorVersion, SessionLocal
    from evidence_attestation import lyric_snapshot_hash
    from quality_learning_jobs import run_correction_observation_job

    snapshot_db = SessionLocal()
    try:
        document = snapshot_db.query(EditorDocument).filter(
            EditorDocument.job_id == job_id,
        ).one()
        version = snapshot_db.query(EditorVersion).filter(
            EditorVersion.id == approved_version_id,
            EditorVersion.job_id == job_id,
            EditorVersion.is_approved.is_(True),
        ).one()
        if int(document.revision or 0) != int(version.revision):
            raise RuntimeError("approved correction snapshot is already stale")
        expected_revision = int(document.revision or 0)
        expected_approved_hash = lyric_snapshot_hash(version.segments or [])
        expected_learning_epoch = int(document.job.quality_learning_epoch or 0)
        # The browser-provided active_edit_ms remains telemetry only. Learning
        # gates consume exclusively contiguous server-side heartbeat evidence.
        server_active_edit_ms = derive_server_active_edit_ms(
            snapshot_db, document.job, version, session_id,
        )
    finally:
        snapshot_db.close()

    queue = Queue("transcription_quality", connection=_redis)
    rq_id = f"correction-learning:{job_id}:{approved_version_id}"
    active = _active_rq_job(_redis, rq_id)
    if active is not None:
        return active.id
    _evict_stale_rq_job(_redis, rq_id)
    queued = queue.enqueue(
        run_correction_observation_job,
        args=(job_id, approved_version_id),
        kwargs={
            "active_edit_ms": server_active_edit_ms,
            "active_edit_source": (
                "server_product_events_v1" if server_active_edit_ms is not None else None
            ),
            "source_confidence": source_confidence,
            "session_hmac": hmac_identifier("editor_session", session_id),
            "expected_revision": expected_revision,
            "expected_approved_hash": expected_approved_hash,
            "expected_learning_epoch": expected_learning_epoch,
        },
        job_timeout=int(os.environ.get("QUALITY_LEARNING_JOB_TIMEOUT", "600")),
        result_ttl=RESULT_TTL, failure_ttl=FAILURE_TTL, job_id=rq_id,
        retry=Retry(max=1, interval=30),
        meta=rq_payload_metadata(
            "correction_learning", approved_version_id=approved_version_id,
        ),
    )
    return queued.id


def ensure_daily_quality_learning_scheduled() -> str | None:
    """Keep exactly one daily miner wake-up in RQ's scheduled registry."""
    # Schedule the wake-up whenever the isolated queue exists. The job itself
    # checks mining's kill switch and re-schedules while disabled, so turning
    # mining back on never depends on a worker restart.
    if not transcription_quality_queue_enabled():
        return None
    _init_redis()
    if _redis is None:
        return None
    from datetime import datetime, timedelta, timezone
    from rq import Queue
    from quality_learning_jobs import run_daily_quality_learning

    queue = Queue("transcription_quality", connection=_redis)
    tomorrow = (datetime.now(timezone.utc) + timedelta(hours=24)).date().isoformat()
    rq_id = f"quality-learning:daily:{tomorrow}"
    active = _active_rq_job(_redis, rq_id)
    if active is not None:
        return active.id
    _evict_stale_rq_job(_redis, rq_id)
    queued = queue.enqueue_in(
        timedelta(hours=24), run_daily_quality_learning,
        job_timeout=int(os.environ.get("QUALITY_LEARNING_MINING_TIMEOUT", "900")),
        result_ttl=RESULT_TTL, failure_ttl=FAILURE_TTL, job_id=rq_id,
        meta=rq_payload_metadata("quality_learning_daily"),
    )
    return queued.id


def enqueue_quality_proposal_validation(proposal_id: str,
                                        experiment_id: str) -> str:
    if not quality_learning_capture_enabled():
        raise RuntimeError("quality learning is disabled")
    if os.environ.get("QUALITY_LEARNING_PROPOSALS_ENABLED", "0").strip().lower() not in {
        "1", "true", "yes", "on",
    }:
        raise RuntimeError("quality learning proposals are disabled")
    if os.environ.get("QUALITY_LEARNING_ABLATIONS_ENABLED", "0").strip().lower() not in {
        "1", "true", "yes", "on",
    }:
        raise RuntimeError("quality learning ablations are disabled")
    _init_redis()
    if _redis is None:
        raise RuntimeError("quality learning queue unavailable")
    from rq import Queue
    from quality_learning_jobs import run_quality_proposal_validation

    queue = Queue("transcription_quality", connection=_redis)
    rq_id = f"quality-learning-validation:{proposal_id}:{experiment_id}"
    queued = queue.enqueue(
        run_quality_proposal_validation, args=(proposal_id, experiment_id),
        job_timeout=int(os.environ.get("QUALITY_LEARNING_VALIDATION_TIMEOUT", "3600")),
        result_ttl=RESULT_TTL, failure_ttl=FAILURE_TTL, job_id=rq_id,
        meta=rq_payload_metadata(
            "quality_learning_validation", proposal_id=proposal_id,
            experiment_id=experiment_id,
        ),
    )
    return queued.id


def enqueue_prores_prewarm(
    job_id: str,
    file_type: str,
    *,
    force: bool = False,
) -> str | None:
    """Schedule the ProRes transcode for `job_id` on the enterprise queue.

    Called from run_pipeline right before the job flips to "done" when
    delivery_profile is umg/both and PRORES_PREWARM is on. The handler
    is `prores.prewarm_prores`, which wraps `ensure_prores_exists` with
    DB lookup. Idempotent against the lazy /download path: whichever
    finishes first wins the os.replace.

    Returns the RQ job id, or None when background prewarm is disabled or
    Redis is unreachable. ``force=True`` is reserved for an explicit user
    action (download/publish): it bypasses the optional prewarm flag and
    queue-depth backpressure, and raises if the queue is unavailable so the
    API can report an honest 503 instead of polling forever.
    """
    _require_submissions_open()
    global prewarm_skipped_total, prewarm_enqueued_total
    if not PRORES_PREWARM_ENABLED and not force:
        return None
    if file_type not in ("umg_master", "umg_short"):
        logger.warning("[PRORES] prewarm: unsupported file_type %r", file_type)
        return None
    _, _, q_enterprise = _init_redis()
    if q_enterprise is None:
        if force:
            raise RuntimeError("ProRes queue unavailable")
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
    if depth > PRORES_PREWARM_MAX_QUEUE_DEPTH and not force:
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
        meta=rq_payload_metadata("prores_prewarm"),
        # Deterministic id: collapses concurrent double-enqueues to one
        # QUEUED entry. NOTE: RQ (1.16.2) does NOT no-op a re-enqueue of a
        # FINISHED id — enqueue overwrites the job hash and re-pushes the id,
        # so it RE-RUNS. run_edit_pipeline relies on exactly that to force a
        # fresh transcode after an edit (it cancel_rq_job's the old one first
        # for hygiene). If RQ is ever upgraded, re-verify this re-run
        # behavior or the post-edit re-warm silently stops firing.
        job_id=f"prewarm:{job_id}:{file_type}",
    )
    prewarm_enqueued_total += 1
    return rq_job.id


def edit_failure_callback(job, connection, type_, value, traceback) -> None:
    """RQ on_failure hook for run_edit_pipeline.

    Fires when retries are EXHAUSTED (PIPELINE_RETRY_MAX consecutive
    worker deaths) or on a real exception inside the pipeline. With
    Retry configured in enqueue_edit (2026-05-26), a single worker
    death no longer surfaces an error — RQ re-enqueues automatically
    and a fresh worker picks the job up after the backoff interval.

    Without this callback, the DB row stays stuck at status='editing'
    for up to 30 min until the reaper catches it — the user sees an
    indefinite spinner with no error.

    Same best-effort contract as pipeline_failure_callback: swallows
    exceptions so RQ's own failure bookkeeping still completes.
    """
    try:
        from jobs import update_job
        edit_id = getattr(job, "id", None) or ""
        # RQ job id is "edit:{job_id}"; strip the prefix to get our job_id.
        rq_job_id = edit_id[len("edit:"):] if edit_id.startswith("edit:") else edit_id
        if not rq_job_id:
            return
        # Surface to Sentry tagged with job/tenant before the DB write.
        _capture_job_failure("edit", rq_job_id, type_, value)
        is_abandoned = "AbandonedJobError" in (type_.__name__ if type_ else "")
        if is_abandoned:
            err_msg = (
                "El servidor se reinició mientras aplicábamos los cambios y "
                "los reintentos automáticos también fallaron. El video "
                "anterior sigue disponible: podés volver a pedir el edit."
            )
        else:
            tb_msg = str(value)[:400] if value else (type_.__name__ if type_ else "error")
            err_msg = f"Edit falló: {tb_msg}"
        # Audit 2026-05-26: route through update_job so we share the FOR
        # UPDATE row lock with the worker (race-safe). update_job's terminal
        # target ("error") always lands, but contends on the lock — if
        # run_edit_pipeline managed to commit `pending_review` first, the
        # worker wins and the user sees the edit they actually got, not
        # a false error.
        update_job(rq_job_id, status="error", error=err_msg[:500])
    except Exception as e:
        logger.warning("edit_failure_callback failed: %s", e)


def enqueue_edit(
    job_id: str,
    edit_type: str,
    edit_params: dict,
    plan: str = "100",
    tenant_id: str = "",
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
    _require_submissions_open()
    from background_policy import runtime_rollout_fingerprint
    _policy_fingerprint = runtime_rollout_fingerprint()
    q = _pick_queue(plan, tenant_id=tenant_id)
    if q is not None:
        from rq import Retry
        from pipeline import run_edit_pipeline
        edit_rq_id = f"edit:{job_id}"
        # Clear any stale RQ job with this ID from a previous failed/completed
        # edit. The 7-day failure_ttl keeps failed jobs in Redis long after
        # they're dead. Without this cleanup, re-enqueue after a worker death
        # silently dedupes (RQ returns/reuses the old failed job) and the DB
        # row stays stuck at status="editing"/progress=0 indefinitely.
        _evict_stale_rq_job(q.connection, edit_rq_id)
        # Retry on worker-death (Railway redeploy/OOM/SIGKILL). Mismo patrón
        # que enqueue_pipeline — incidente 2026-05-26 con el job de Los
        # Abuelos (4c47a9f07383): el merge de fix(pipeline) a staging
        # disparó un redeploy del Worker mientras el edit estaba mid-render,
        # AbandonedJobError, edit_failure_callback flipea status="error" y
        # el operador queda con "El servidor se reinició mientras
        # aplicábamos los cambios" sin retry automático. Con Retry, RQ re-
        # encola el job y un worker fresco lo retoma a los 60s; run_edit_
        # pipeline es safe re-runnable (lee state de DB cada vez, Veo está
        # cacheado por hash de prompt, libass/R2 sobreescribe).
        retry = Retry(max=PIPELINE_RETRY_MAX, interval=PIPELINE_RETRY_INTERVAL_S)
        rq_job = q.enqueue(
            run_edit_pipeline,
            args=(job_id, edit_type, edit_params, _policy_fingerprint),
            # 60 min — covers worst-case long-song edits with motion enabled
            # until we land the ffmpeg-overlay rewrite.
            job_timeout=3600,
            result_ttl=RESULT_TTL,
            failure_ttl=FAILURE_TTL,
            job_id=edit_rq_id,
            meta=rq_payload_metadata(
                "edit", background_policy_fingerprint=_policy_fingerprint,
            ),
            retry=retry,
            on_failure=edit_failure_callback,
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
        args=(job_id, edit_type, edit_params, _policy_fingerprint),
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
    _require_submissions_open()
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
        meta=rq_payload_metadata("drive_delivery"),
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
