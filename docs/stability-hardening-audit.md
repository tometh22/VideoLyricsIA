# Stability hardening — architecture audit & rollout

**Context.** The app "se sobrecarga fácil, se pone lenta, los procesos se cuelgan",
and Railway has recurring private-networking blips (e.g. *"Private Networking
intermittent connectivity issues in US West"*, 2026-06-04). A deep read-only audit
(5 senior specialists, one per dimension) found that the **majority of the pain is
code-level, not the platform** — i.e. it would follow us to GCP/DO. A small subset
is what turns a Railway infra blip into a user-visible outage; that subset is cheap
to fix and is **Tier 1**.

This doc is the tracking artifact. Each tier ships as its own focused branch off
`staging` → PR → soak → normal `staging`→`main` (prod) promotion. Nothing touches
`main` directly.

---

## Root-cause summary (audit)

Three independent auditors converged on the same headline: the API uses **synchronous
SQLAlchemy** but exposes **92/93 routes as `async def`**, running blocking DB/Stripe/auth
work **directly on the event loop** of only **4 processes** (2 replicas × 2 uvicorn
workers). Any slow dependency freezes the whole process → "todo lento a la vez". The
worker side has a hard ceiling of **7 concurrent renders** with long/short jobs sharing
slots (head-of-line blocking), plus retry-storm amplification on Veo 429.

| Symptom | Root cause | Evidence |
|---|---|---|
| Lento/colgado de golpe | Sync DB + Stripe + auth bloquean el event loop en `async def` | `main.py` 92 async handlers; `auth.py:594`; `billing.py:173/230` |
| Se sobrecarga fácil | Techo de 7 renders; colas largas y cortas comparten slots | `worker.py:164-173,225` |
| Procesos colgados | Veo 429 → worker duerme ~10 min reteniendo el slot + retry storm RQ | `pipeline.py:6114-6162`; `queue_jobs.py:40-45` |
| Outage en blip de Railway | Healthcheck profundo + faltan timeouts (DB connect / Redis worker / OAuth) | `railway.toml:71`; `database.py:86`; `worker.py:153`; `pipeline.py:5780` |

What's **already good** (do not redo): R2 streamed/multipart upload + timeouts; Veo poll
600s deadline + backoff/jitter; all RQ jobs have explicit timeouts; DB pool well-tuned
(`pool_pre_ping`, `pool_recycle=120`, keepalives); liveness/readiness split already exists;
reaper + retry + terminal-state guards solid.

---

## Rollout — one branch per risk tier (off `staging`)

| Tier | Branch | Findings | Risk | UX change |
|---|---|---|---|---|
| **1** | `perf/t1-railway-resilience` | C4 DB connect_timeout · M1 worker Redis keepalive · H4 OAuth refresh timeout | Muy bajo | No |
| 2 | `perf/t2-async-eventloop` | C1/C2 polled+SSE+auth handlers → `def`/threadpool · H1 Stripe offload | Medio (flag-gated) | No (equivalente) |
| 3 | `perf/t3-worker-queues` | C3 dedicated short-queue workers · M6 admission control · M2 reaper threshold/isolation | Medio (env-gated) | Mejora latencia |
| 4 | `perf/t4-render-resources` | C5 disk-leak en path de fallo · H6 cap `-threads` ffmpeg · H3 fix `_call_with_timeout` · M4 subprocess encode | Bajo-medio | No |
| 5 | `perf/t5-db-queries` | H5 N+1 admin (`admin.py:156/669/80`) · M3/M5 · enable `statement_timeout` | Bajo | No |

Principles every tier follows: env kill-switch defaulted to current behavior; tests +
`make check` green per PR; draft PR to `staging`, soak before ready; cut from clean
`origin/staging`.

---

## Tier 1 — Railway-blip resilience  ·  status: **IN PROGRESS**

All three are *fail-fast on a network call that today has no timeout*, so a Railway blip
hangs the thread/worker indefinitely. Zero change to normal operation — only changes what
happens **during** a blip.

- [x] **C4 — DB `connect_timeout`.** `database._build_pg_connect_args()` adds
      `connect_timeout=5` (env `DB_CONNECT_TIMEOUT`). Bounds `engine.connect()` (used by the
      `/health` probe and every fresh pool checkout) so a blip can't exceed
      `healthcheckTimeout=90` and get a healthy replica pulled from rotation.
- [x] **M1 — worker Redis resilience.** `worker._redis_connect_kwargs()` adds
      `socket_connect_timeout=5` + `socket_keepalive` + tuned `TCP_KEEP*` (Linux-guarded) +
      `health_check_interval=30`. **Deliberately NO `socket_timeout`** — the worker's blocking
      `BLPOP` would time out spuriously; TCP keepalive detects a dead peer in ~30-60s without
      breaking dequeue. (Diverges from the audit's literal "mirror the enqueue client" for
      correctness — the enqueue client does quick ops, the worker blocks on BLPOP.)
- [x] **H4 — OAuth refresh timeout.** `pipeline._oauth_refresh_request()` binds the
      google-auth refresh to a `requests.Session` with a default `timeout`
      (env `VEO_OAUTH_REFRESH_TIMEOUT`, default 10s) instead of the 120s transport default.
      Used by `_veo_access_token()` (every Veo submit/poll/download) and `_get_genai_client()`.
- [x] Tests: `tests/test_tier1_resilience.py` (wiring/presence guards). `make check` green;
      existing worker/health tests green; zero new lint findings.
- [ ] PR (draft) → `staging`, soak.
- [ ] Promote `staging` → `main` (prod).

### Env knobs introduced (all default to current/safe behavior)
`DB_CONNECT_TIMEOUT=5` · `REDIS_SOCKET_CONNECT_TIMEOUT=5` · `REDIS_HEALTH_CHECK_INTERVAL=30` ·
`REDIS_TCP_KEEPIDLE=30` · `REDIS_TCP_KEEPINTVL=10` · `REDIS_TCP_KEEPCNT=3` ·
`VEO_OAUTH_REFRESH_TIMEOUT=10`

### Explicitly deferred from Tier 1 (rationale)
- **Repointing Railway `healthcheckPath` to `/health/live`** — changes deploy-gate
  semantics; the `connect_timeout` already removes the hang mechanism. Revisit separately.
- **`statement_timeout`** — would kill the slow admin N+1 queries until Tier 5 fixes them.
- **OAuth token caching** — an optimization, not a resilience fix; the timeout is the win.

---

## Tier 2 — Async/event-loop unblocking  ·  branch `perf/t2-async-eventloop`  ·  status: **CODE COMPLETE (pre-review)**

The tangible-impact tier: gets blocking DB/Stripe work **off the uvicorn event loop**.
Reversibility is per-commit `git revert` (async→def can't be env-flagged) + logic-identical
change + tests + soak. Three independently-revertable commits.

- [x] **T2a — `def` conversion (the win).** `get_current_user` (`auth.py:594`) + 16 hot/polled
      `main.py` handlers (`status`, `telemetry_heartbeat`, `jobs`, `transcription_status`,
      `usage`, `me`, `refresh_token`, `settings` GET/POST, `drive_status`,
      `get_drive_transfer`, `backgrounds` ×2, `admin_queue`, `delivery_profiles`, `fonts`)
      converted `async def`→`def`. Each pre-verified to contain no `await`. FastAPI runs them
      in the threadpool, off the loop.
- [x] **T2b — SSE `/events` off-loop.** Per-tick JWT decode + 2 DB queries extracted to sync
      `_sse_tick`, run via `asyncio.to_thread`; generator stays async. Per-tick tenant
      re-validation (security, PR #95) preserved, moved into the thread.
- [x] **T2c — Stripe offload + config.** 9 billing handlers `async def`→`def` (covers ALL
      Stripe+DB calls in one move — more than the audit's 7); `stripe_webhook` stays async with
      DB dispatch in sync `_dispatch_webhook_event` via `to_thread`; global
      `stripe.max_network_retries=2` + version-tolerant HTTP timeout.
- [x] Tests: `tests/test_tier2_eventloop.py` (29 guards via `inspect.iscoroutinefunction`).
      `make check` green; **zero new lint**; full existing suite diff vs baseline = **identical
      failure set (6 pre-existing staging failures, none introduced)**.
- [ ] PR (draft) → `staging`; load-test (`/health/live` latency flat under concurrent
      DB-touching load); soak; promote to `main`.

### Env knobs introduced (safe defaults)
`STRIPE_MAX_NETWORK_RETRIES=2` · `STRIPE_HTTP_TIMEOUT=15`

### Implementation divergences from the plan (both safer/cleaner)
- **Worker socket_timeout** (Tier 1) was dropped for TCP keepalive — already noted there.
- **Stripe** (T2c): the plan said "wrap 7 calls in `to_thread`"; the branch instead **converts
  the 9 no-`await` billing handlers to `def`**, which offloads *every* Stripe+DB call at once
  (the audit undercounted the call sites). Only `stripe_webhook` (awaits `request.body()`) keeps
  the `to_thread` wrap. Less code, same proven pattern, broader coverage.

### Note
- 6 failing tests on `origin/staging` are **pre-existing** (flaky/broken before this branch);
  not in scope here. Verified by running the existing suite on clean `origin/staging`.
