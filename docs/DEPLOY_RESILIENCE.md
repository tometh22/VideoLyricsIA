# Deploy Resilience Runbook

> What protects in-flight video renders from Railway redeploys, OOMs, and
> hard worker crashes. If you're reading this because users reported
> stuck videos, also see `RUNBOOK_EMERGENCY.md`.

## The failure we're defending against

Veo background generation polls the Vertex API for 60–120 s per job.
While the polling loop runs inside the worker process, anything that
kills the container hard — Railway redeploy on `git push`, OOM, manual
restart, platform-side maintenance — orphans the job:

- Postgres `jobs` row stays at `status='processing'`.
- `ai_provenance` row for the in-flight Veo call stays at
  `duration_ms IS NULL`.
- User sees a forever-spinning "Generando" until something cleans it up.

This page documents the four layers that turn that incident into a
non-event.

## Layer 1 — Graceful shutdown (worker.py)

RQ's `Worker.request_stop()` handler is installed in `worker.py` for
SIGTERM and SIGINT. On signal, the worker stops accepting new jobs and
finishes the current one before exiting.

For this to actually help during a Railway redeploy, the **Railway
shutdown grace period must be longer than the worst-case in-flight job
step**. Veo's p99 is ~2 min. Default Railway grace is 30 s, which is
worse than nothing — RQ acknowledges SIGTERM, stays alive trying to
finish the Veo poll, then SIGKILL fires at 30 s and the job is killed
even though it would have completed at 90 s.

**Action item:** set on the Worker service:

    RAILWAY_SHUTDOWN_TIMEOUT_SECONDS=180

In the Railway dashboard → Worker service → Variables. 3 minutes covers
Veo p99 + a small safety buffer.

## Layer 2 — RQ Retry (queue_jobs.py)

`enqueue_pipeline` attaches `Retry(max=PIPELINE_RETRY_MAX, interval=...)`
to every pipeline job. When a worker dies and RQ's `cleanup_ghosts`
moves the abandoned job to FailedJobRegistry, the Retry mechanism
re-enqueues it once (with a 30 s pause so the replacement pod has time
to boot).

Tunable env vars (defaults are good for current traffic):

- `PIPELINE_RETRY_MAX=1` — number of retries after the first attempt.
  Bumping to 2 buys safety for back-to-back redeploys; never set above
  3 unless you have evidence the infrastructure is unhealthy.
- `PIPELINE_RETRY_INTERVAL_S=30` — backoff between attempts.

The retry runs `run_pipeline` from scratch. Veo backgrounds are cached
in R2 by prompt hash (see `pipeline.py:[BG] Veo cache STORED`), so the
retry usually skips paying for a second Veo generation — only the
cheap post-Veo steps re-execute.

## Layer 3 — Failure callback (queue_jobs.py)

When all retries are exhausted (real pathological case — RQ has given
up), `pipeline_failure_callback` fires. It flips the Postgres row to
`status='error'` with a Spanish, user-actionable message pointing at
the **Reintentar sin re-subir** button in the UI (`/retry/{job_id}`
endpoint, which reuses the audio still stored in R2). The user
recovers with one click.

If the failure was an `AbandonedJobError` (worker died), the message
explains that the server restarted. If it was a real exception, the
message includes a short version of the traceback so support can
debug.

## Layer 4 — Orphan reaper (reaper.py)

The reaper sweep runs every 5 minutes from `main.py:_reaper_loop`. It
has **two** detectors:

- **Age-based** (`find_stuck_jobs`, threshold `REAPER_THRESHOLD_MIN`,
  default 100 min). Conservative — catches everything eventually.
- **Provenance-based** (`find_orphan_polling_jobs`, threshold
  `REAPER_ORPHAN_POLL_THRESHOLD_MIN`, default 10 min). Catches the
  deploy-death signature precisely: a Job row in `processing` whose
  latest `ai_provenance` row has `duration_ms IS NULL` and is older
  than the threshold. Veo p99 is ~2 min, so 10 min is solidly past
  "still healthy" without being so eager that it kills slow-but-fine
  calls.

Either way, the reaped row gets the same Spanish error message and the
`/retry` button surfaces in the UI.

## How the layers compose

| Failure mode                         | First responder        | Time to user-visible error |
|--------------------------------------|------------------------|----------------------------|
| Planned redeploy, fast pod boot      | Layer 1 (grace) → no error | n/a (job finishes) |
| Planned redeploy, slow pod boot      | Layer 2 (RQ Retry)     | 30 s (interval) + retry runtime |
| Hard SIGKILL, retry also dies        | Layer 3 (failure cb)   | Immediate when RQ gives up |
| Worker process leak, no signal at all| Layer 4 (orphan sweep) | up to 10 min |
| Postgres `processing` row, no RQ trace | Layer 4 (age sweep)  | up to 100 min |

## Configuration in Railway dashboard

These can't be expressed in `railway.toml` and need to be set manually:

1. **Pro plan or higher** — required for overlap-window rolling deploys
   (new replica boots and goes healthy BEFORE the old one is killed).
   Hobby/Starter plans do kill-then-spawn regardless of config.

2. **`RAILWAY_SHUTDOWN_TIMEOUT_SECONDS=180`** on the Worker service.
   See Layer 1 above. Without this, Layer 1 is a no-op.

3. **Health check path** — leave blank on the Worker service (it's a
   daemon, not HTTP). Set on the API service to `/health`.

4. **Worker concurrency** — if you scale to >1 worker replica, each one
   gets its own SIGTERM handler. The reaper uses a Postgres advisory
   lock (`_REAPER_ADVISORY_LOCK_KEY`) so the cleanup pass runs on
   exactly one replica per cycle regardless of how many workers exist.

## Verifying the fixes work

1. Trigger a Veo render via the UI.
2. While the job is at `current_step='background'` / `progress=22`, run
   `railway service restart Worker` in another terminal.
3. Expected: the job either (a) finishes after the new worker picks it
   up via Retry, or (b) shows `error` with the Reintentar button within
   10 minutes (Layer 4 backup).
4. **Not expected:** the job sits at `processing/background/22` for an
   hour or more. If it does, check `_reaper_loop` is running
   (`/admin/runbook/reaper-now` to force a sweep) and that the new
   worker pod is actually healthy in Railway logs.
