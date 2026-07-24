# Scaling Plan

> What breaks first as concurrent-user load grows, in what order to fix
> it, and which knobs to turn at each step. Pair this with
> `DEPLOY_RESILIENCE.md` (failure recovery) — they cover orthogonal
> problems.

The premise: you want this product to serve many more concurrent
users than the 1–5 admin/operator load it was designed against.
The good news is that the architecture (FastAPI + RQ + Postgres +
R2 + Vertex) scales horizontally cleanly. The bad news is that
every layer has a different ceiling, and they don't all warn you
politely before they hit it.

## Current bottleneck order

Tested by following the path of a single render job:

| # | Ceiling | When it bites | Fix |
|---|---------|---------------|-----|
| 1 | **DB pool per process** | ~10 concurrent short queries OR 1–2 concurrent slow streams | Already fixed via `scoped_db()` — see below |
| 2 | **Postgres max_connections** | ~80 sockets across all processes (default 100, minus admin/reaper headroom) | Add PgBouncer in front, or bump DB plan |
| 3 | **Veo per-project quota** | Vertex AI throttles around N concurrent video generations | Request a quota bump or split projects per tenant |
| 4 | **RQ worker count** | 4 workers × ~5 min/job = 48 jobs/hour. UMG batches of 5 hit the queue instantly | Scale Worker service horizontally on Railway |
| 5 | **R2 egress bandwidth** | Multi-GB ProRes downloads queue at Cloudflare's per-account limits | Use Cloudflare paid plan tiers or staggered downloads |
| 6 | **uvicorn workers** | CPU-bound endpoints (`/auth/login` bcrypt, `/transcribe` if local Whisper still on) | Bump `WEB_CONCURRENCY` |

Each numbered item only matters once the lower-numbered ones are
sorted. Don't pre-optimize against #5 while #2 is still 30% of the
way to its limit.

## 1. DB pool per process — fixed

**Pre-fix:** Streaming endpoints (`/preview/`, `/download/`,
`/backgrounds/.../preview`, `/jobs/.../events` SSE,
`/download/.../all` ZIP) held a pool slot via `Depends(get_db)` for
the *full duration* of the HTTP response. SSE for a 60-min render =
60 minutes of one socket gone. With 8 sockets per process, a single
operator with 6 dashboard cards open could lock the entire API.

**Fix (this PR):** Those endpoints now use `scoped_db()` from
`database.py`, which opens a session for the metadata reads and
closes it BEFORE the file/SSE handoff. The pool slot is reused
within milliseconds instead of being held for the stream lifetime.

**Net:** per-process concurrency went from "≤10 mixed requests" to
"≤10 concurrent short queries + unbounded concurrent streams". This
is the single biggest jump in capacity in this doc.

**Defaults:** `DB_POOL_SIZE=6`, `DB_MAX_OVERFLOW=4` per process. With
8 processes (4 API + 4 RQ workers) the steady ceiling is 48 sockets,
peaks to 80 — well under Postgres `max_connections=100`.

**Monitoring:** `/health` returns `db_pool` with `in_use / total / utilization`.
Alert when utilization stays above 0.8 for 5+ minutes — that's the
signal you're close to bottleneck #2.

## 2. Postgres max_connections (next ceiling)

Once `/health` shows db_pool utilization regularly hitting 0.6–0.8,
you're approaching the absolute ceiling of Railway's default Postgres
plan (100 connections).

**Options, in order of cost:**

1. **Raise `max_connections` on Postgres.** Free if you have an
   instance with spare RAM (~10 MB / connection). Risky if you don't —
   too many connections starves PG's other workers (autovacuum,
   checkpointer).

2. **Put PgBouncer in front of Postgres.** Free open-source. Each app
   process opens 1 PgBouncer socket; PgBouncer multiplexes onto the
   real PG. Lifts the per-app-process ceiling from "your process count"
   to "PgBouncer's pool size". Required when you cross ~10 processes.

3. **Bigger DB plan.** Railway Pro plans support higher
   `max_connections` by default. Pay for capacity.

Recommended order at scale: #2 first (PgBouncer), then #3 when even
PgBouncer's pool starts to fill.

## 3. Veo per-project quota

Vertex AI throttles `veo-3.1-fast-generate-001` concurrent generations
per project. The current project is shared by all renders. If 10
operators each kick off a batch of 5 jobs simultaneously, queue depth =
50, all of them lined up for Veo. Vertex starts returning 429.

**Mitigation:**

- The reaper + RQ Retry (`docs/DEPLOY_RESILIENCE.md`) absorbs 429s as
  transient failures — the job retries after backoff. So you get
  *slowness*, not data loss.
- For more headroom: request a quota bump from Google Cloud (free,
  takes 1–3 days) or split high-volume tenants onto their own Vertex
  projects via `VERTEX_PROJECT` env per worker.

**Monitoring:** count provenance rows for `tool_name LIKE 'veo-%'`
that hit the retry path. Sustained retries means you're throttled.

## 4. RQ worker scaling

Each Worker pod has `WORKER_MAX_JOBS=10` and exits after that (moviepy
memory leak mitigation — see `worker.py`). Railway respawns a fresh
one within ~30 s.

Steady-state throughput at 4 workers, ~5 min per job:
- 4 × 12 jobs/hour = **~48 jobs/hour**.

Above that, queue depth grows. The user's experience is "submit ⇒
wait several minutes ⇒ Veo starts". They never see an error, just
delay.

**Mitigation:** scale the Worker service horizontally on Railway.
Each extra replica adds another 12 jobs/hour. The reaper's Postgres
advisory lock prevents duplicate sweeps regardless of replica count.
Cap at the number of Veo quota slots (bottleneck #3) — more workers
than Veo can serve just queues at Vertex instead of at RQ.

## 5. R2 egress bandwidth

ProRes masters are 1–5 GB. The streaming endpoints redirect to
pre-signed R2 URLs so uvicorn is never the bottleneck — Cloudflare
serves the bytes. But R2 has per-account bandwidth tiers.

**Monitoring:** Cloudflare dashboard → R2 → Bandwidth.

**Mitigation when you hit the tier ceiling:** upgrade R2 plan
(transparent, no code change), or stagger UMG batch downloads (most
direct controllable).

## 6. uvicorn worker count

Set `WEB_CONCURRENCY=4` (Railway default for current dyno). If you
see API endpoints (`/auth/login`, `/transcribe` legacy path) blocking
on CPU, bump to 6 or 8. Each uvicorn worker adds DB pool sockets
proportionally — verify against bottleneck #2 first.

## A note on observability

You can't manage what you don't measure. Before turning any knob:

1. Open `/health` periodically; eyeball `db_pool.utilization`.
2. Postgres: `SELECT count(*) FROM pg_stat_activity WHERE state != 'idle';`
   tells you live active queries.
3. Worker logs in Railway: queue depth (`enterprise: ` or `default: `
   lines) shows backlog.
4. Sentry: rate of `TimeoutError: QueuePool` is the canary for
   bottleneck #1; rate of `vertexai.types.exceptions` is the canary
   for #3.

The pattern is always the same: a sustained alert on one of these is
the signal to apply the next item in the bottleneck table — never
two items ahead.
