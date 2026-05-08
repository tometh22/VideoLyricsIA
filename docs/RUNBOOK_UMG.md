# RUNBOOK — UMG production readiness

Concrete checklist for the first paying customer (UMG / Universal Music)
to land safely. Walks through Railway settings, env vars per service,
the one-shot Alembic bootstrap, and a smoke procedure to verify before
their first batch.

---

## 1. Railway infrastructure

Two services minimum: **API** (uvicorn) + **Worker** (RQ). Optionally a
third for the cron / scheduled tasks, but the current code runs the
reaper + outputs-cleanup loops as daemon threads inside the API
container, so a separate cron service is NOT required.

### API service

| Setting | Value | Why |
|---|---|---|
| Plan | **Pro** (or higher) | Need horizontal scale support |
| Replicas | 1-2 | More replicas need the reaper advisory lock (already in code) |
| vCPU per instance | 2 | API is mostly I/O-bound |
| RAM per instance | 1 GB | Comfortable for FastAPI + JWT + 4 uvicorn workers |
| Disk | **default is fine** (8 GB) | API doesn't write multi-GB files |
| Healthcheck path | `/health` | Returns 503 when down, 200 otherwise |
| Release command | `bash scripts/prod_migrate.sh` | Runs `alembic upgrade head` before swapping the image |

### Worker service

| Setting | Value | Why |
|---|---|---|
| Plan | **Pro** | Same |
| Replicas | 2-3 | More = faster queue drain. Each handles 1 ffmpeg at a time |
| vCPU per instance | **4-8** | 4K@60 ffmpeg needs ~2 vCPU; with 1 worker per replica, 4 vCPU is comfortable, 8 is wide margin |
| RAM per instance | **2-4 GB** | moviepy 4K renders peak around 1.5 GB; 4 GB has margin |
| Disk | **100 GB** | A 4K@60 burst can use 30-40 GB temp space simultaneously |
| Healthcheck | (none) | RQ worker isn't an HTTP service |
| Start command | `python worker.py` | |

### Postgres addon

| Setting | Value | Why |
|---|---|---|
| `max_connections` | **200** | API+Worker peak 112 sockets at burst; 100 is too tight |
| Storage | **20 GB** | Provenance + jobs grow linearly; 20 GB lasts ~2 years at 250/mo |
| Backups | Daily | Railway Pro includes this |

To raise `max_connections`: Railway dashboard → Postgres service → Variables → set `PG_MAX_CONNECTIONS=200`. Verify after restart with:
```sql
SHOW max_connections;
```

### Redis addon

Defaults are fine. RQ is the only consumer; the queue depth never gets
big enough to matter for memory.

### R2 bucket

Storage is unmetered for the kind of volume this account will see (250
videos/month × ~3 GB master ≈ 750 GB). Cloudflare R2 has free egress;
no CDN configuration needed for first year.

---

## 2. Env vars — copy these into Railway

### Both services (API + Worker)

```bash
ENVIRONMENT=production
DATABASE_URL=${{Postgres.DATABASE_URL}}
REDIS_URL=${{Redis.REDIS_URL}}
DB_POOL_SIZE=8
DB_MAX_OVERFLOW=8
SENTRY_DSN=https://<your-key>@sentry.io/<project>
OWNER_EMAIL=tomi@<yourdomain>
ADMIN_EMAIL=tomi@<yourdomain>
```

### API only

```bash
JWT_SECRET=<run: openssl rand -base64 32>
CORS_ORIGINS=https://app.<yourdomain>.com,https://staging.<yourdomain>.com
ADMIN_PASSWORD=<run: openssl rand -base64 24>     # only used the first time the DB is empty
STRIPE_WEBHOOK_SECRET=whsec_...                    # only if Stripe is wired
STRIPE_SECRET_KEY=sk_live_...
STRIPE_PRICE_100=price_...
STRIPE_PRICE_250=price_...
STRIPE_PRICE_500=price_...
STRIPE_PRICE_1000=price_...
FRONTEND_URL=https://app.<yourdomain>.com
MIN_FREE_DISK_GB_FOR_UPLOAD=10
MEDIA_TOKEN_EXPIRE_SECONDS=300

# UMG-specific operating limits — see section 4 below
TENANT_BACKLOG_LIMIT=15
DEFAULT_DAILY_CAP=200
GLOBAL_MAX_PROCESSING=20

# R2 storage
R2_ACCESS_KEY_ID=...
R2_SECRET_ACCESS_KEY=...
R2_ENDPOINT_URL=https://<account>.r2.cloudflarestorage.com
R2_BUCKET=lyricgen-prod
```

### Worker only

```bash
OPENAI_API_KEY=sk-...                              # skips local Whisper, halves RAM
VERTEX_PROJECT=lyricgen-prod
VERTEX_LOCATION=us-central1
VEO_MODEL=veo-3.1-fast-generate-001                # default — change to non-fast for HQ jobs
GEMINI_API_KEY=...                                  # alt to Vertex
WORKER_MAX_JOBS=5                                  # recycle every 5 jobs (4K@60 leaks more)
JOB_TIMEOUT_SECONDS=2700                           # 45 min YouTube
JOB_TIMEOUT_UMG_SECONDS=7200                       # 2 h UMG (4K@60 needs more)
PRORES_PREWARM=1
PRORES_PREWARM_TIMEOUT_SECONDS=900
PRORES_PREWARM_MAX_QUEUE_DEPTH=20

# R2 (worker uploads + downloads)
R2_ACCESS_KEY_ID=...     # same as API
R2_SECRET_ACCESS_KEY=...
R2_ENDPOINT_URL=...
R2_BUCKET=lyricgen-prod

# Cleanup loop tunables (see section 5)
CLEANUP_KEEP_DONE_MIN=1440
CLEANUP_RETRY_FAILED_MIN=60
CLEANUP_DRY_RUN=1                                   # ⚠️ first week only, then set to 0
```

### Vertex credentials JSON

If you upload Vertex credentials as a multi-line env var in Railway:

```bash
VERTEX_CREDENTIALS_JSON='{"type":"service_account",...}'
```

`credentials_bootstrap.py` reads this and writes it to a temp file at
worker startup. If you'd rather mount a file: put it at
`/app/backend/vertex_credentials.json` and skip the env var.

---

## 3. One-time Alembic bootstrap (first deploy)

Migrations live in `lyricgen/backend/alembic/versions/`. The release
command (`scripts/prod_migrate.sh`) runs `alembic upgrade head` on every
deploy.

**Before the very first deploy with this PR**, you need to mark your
existing prod schema as already-migrated:

1. Get the prod `DATABASE_URL` from Railway (Postgres service →
   Connect → External URL).
2. From your laptop:
   ```bash
   cd lyricgen/backend
   pip install alembic sqlalchemy psycopg2-binary
   DATABASE_URL="<paste prod url>" alembic stamp head
   ```
3. Confirm:
   ```bash
   DATABASE_URL="<paste prod url>" alembic current
   # should show: a71feb1a87dc (head)
   ```
4. Now deploy. The release command will run `upgrade head` and find
   nothing to do — all subsequent schema changes go through Alembic.

**Why stamp instead of upgrade**: the initial migration contains
`CREATE TABLE` statements for every existing table. Running `upgrade
head` against a DB that already has those tables would fail with
"table already exists". `stamp` skips the SQL and just records the
revision in `alembic_version`.

**For a brand-new staging DB**: skip step 2, just deploy. Railway
release command runs `upgrade head` which creates everything from
scratch.

---

## 4. UMG operating limits — choose one of two configs

The defaults in `main.py` were sized for a 1-tenant/1-user launch. UMG
brings 3 simultaneous users, so the caps need adjustment.

### Config A: Warner = 1 tenant, 3 users share one workspace

This is the **recommended** model for label-team operation: all users
under `tenant_id="warner_music"` see each other's jobs (UI surfaces a
shared queue).

```bash
TENANT_BACKLOG_LIMIT=15        # 3 users × 5 in-flight each
DEFAULT_DAILY_CAP=200          # 100/day peak day with margin
GLOBAL_MAX_PROCESSING=20       # global cap across ALL tenants
```

When you create the 3 users via `/admin/users`, pass
`tenant_id=warner_music` for each.

### Config B: each user is their own tenant

Pick this only if Warner explicitly asks for siloed workspaces (rare).
Each user gets their own `tenant_id`, defaults work as-is, but you'd
still want:

```bash
DEFAULT_DAILY_CAP=200
GLOBAL_MAX_PROCESSING=20
```

`TENANT_BACKLOG_LIMIT=5` (default) is fine because each user has their
own slot.

---

## 5. Outputs cleanup — trust but verify

The hourly cleanup loop (P2) deletes local files from `outputs/` once
they're on R2. **Run with `CLEANUP_DRY_RUN=1` for the first week** and
watch the worker logs:

```
[dry-run] done + R2 complete (age 1450 min): /app/outputs/job_abc123 (3014892152 bytes)
```

If every line looks correct (only deletes done jobs older than 24 h
that have full s3_keys, never touches running jobs), flip the env var:

```bash
CLEANUP_DRY_RUN=0
```

Disk free is reported at `/health.disk_free_gb`. Below 5 GB the
upload gate kicks in (P5) → 503 + Retry-After to clients. The cleanup
loop should reclaim space within an hour; clients retry and succeed.

---

## 6. Smoke procedure before UMG's first batch

Run these in order, ~30 min total. If any step fails, **don't ship**.

### 6.1 Backend health

```bash
curl -s https://api.<yourdomain>.com/health | jq
```

Expected:
```json
{
  "status": "ok",
  "env": "production",
  "disk_free_gb": 95.3,
  "db": "up",
  "db_pool": {"in_use": 0, "total": 16, "utilization": 0.0},
  "redis": "up",
  "queue_depth": {"enterprise": 0, "default": 0},
  "workers_alive": 2,
  "r2": "configured"
}
```

If `workers_alive: 0`, the worker service didn't boot. Check Railway
logs.

### 6.2 Smoke render — HD@24 single job

Upload one MP3 via the UI as a test user, `delivery_profile=umg`,
HD/24/profile 3. Expected timeline:
- Upload → status="queued" within 1 s
- Render → status="done" within 3 min
- Worker log shows `[PRORES] master ready: <size> MB` for both master
  and short within 90 s of "done"
- Click "Master ProRes" → 302 to R2 → file downloads in <30 s

### 6.3 Smoke render — UHD-4K@60 single job

Same as above with 4K@60. Expected timeline:
- Render → status="done" within 8 min
- Prewarm → ready within 5 min after that
- Click "Master ProRes" → 302 to R2 → file downloads in 1-3 min
- ffprobe the .mov:
  ```
  codec_name=prores, profile=HQ, width=3840, height=2160, r_frame_rate=60/1
  ```

### 6.4 Smoke concurrency — 5 parallel uploads

Same test user, fire 5 uploads back-to-back via the UI. Expected:
- All 5 land in queue
- Worker processes them sequentially (3 concurrent if 3 replicas)
- All reach "done" within 25 min
- All 5 prewarms eventually complete
- Click "Master ProRes" on each → all instant 302 to R2

If anything 5xx's: open the worker log and check for the actual error.
P1's 202 polling means transient errors look like long waits, not 5xx.

### 6.5 Smoke concurrency — simulate UMG batch

`stress_umg_full.py` runs the full flow:

```bash
cd lyricgen/backend
python scripts/stress_umg_full.py \
  --base-url https://api.<yourdomain>.com \
  --tenants 2 --songs-per-tenant 5 \
  --umg-spec UHD-4K,60,3 \
  --max-render-min 60
```

Pass criteria printed at end:
- Zero HTTP 5xx
- All renders done within 60 min
- Peak DB pool utilization < 0.80

---

## 7. Day-of-launch monitoring

For the first 30 minutes after UMG's first real batch:

1. **Watch `/health` every 60 s** — sample with `watch -n 60 'curl -s
   https://api.<yourdomain>.com/health | jq'`. Look for status flips,
   db_pool utilization, queue_depth growth.

2. **Watch worker logs** — Railway dashboard → Worker → Logs. Look
   for:
   - `[PIPELINE]` start lines for each job
   - `[PRORES] master ready: <MB>` for each prewarm
   - No `WARNING: source mismatch may produce frame-rate-conversion
     artifacts` lines (means PR #9's pure-recode path is working)
   - No exceptions / tracebacks

3. **Watch Sentry** — any `pipeline.failed` or `reaper.killed` alert
   means a job is having trouble. The user-facing experience is OK
   (job marks as error), but you should know.

4. **Spot-check 1 master file** — once UMG approves their first job,
   `ffprobe` the .mov (or have UMG send it back) and confirm:
   ```
   codec_name=prores
   profile=HQ
   pix_fmt=yuv422p10le
   color_space=bt709
   r_frame_rate=<exact rational, e.g. 24000/1001 for 23.976>
   audio: pcm_s24le, 48000 Hz, 2 ch
   ```

---

## 8. Things that should never happen (escalate immediately)

| Symptom | Likely cause | Action |
|---|---|---|
| `/health` returns 503 | Postgres or Redis unreachable | Check Railway addon status |
| Multiple jobs stuck > 100 min in "processing" | Worker crashed mid-render | Reaper should auto-flip to "error" within 5 min; check `Sentry` for OOM |
| `db_pool.utilization > 0.95` for sustained periods | Pool exhausted | Restart API service; if recurrent, raise `DB_POOL_SIZE` and Postgres `max_connections` |
| Disk free < 1 GB | Cleanup loop not keeping up | Run `python scripts/cleanup_old_outputs.py` manually with `CLEANUP_DRY_RUN=0`; check why R2 uploads are failing |
| 5xx on `/download/{id}/umg_master` | Bug — `check_prores_readiness` should never 5xx | Sentry has the trace; revert if needed |
| `prores_prewarm.skipped_total` rising fast | Queue saturating | Ok in isolation; if sustained, scale up workers |

---

## 9. Day-2 maintenance

### Rotating JWT_SECRET

Tokens are signed by it; rotating invalidates every active session. Do
this only after a compromise. Swap the env var → restart API → notify
users to re-login.

### Rotating ADMIN_PASSWORD

Only matters during initial bootstrap. After the first admin user
exists, this var is ignored.

### Backups & restore

Railway Postgres takes daily snapshots automatically. To restore: open
the addon page, pick a snapshot, click Restore. R2 has lifecycle rules
configured by default; jobs older than 1 year can be policy-deleted if
storage cost ever becomes an issue (it won't at 250/mo).

### Schema changes (developer side)

```bash
cd lyricgen/backend
# 1. Edit the SQLAlchemy model in database.py
# 2. Generate the migration:
DATABASE_URL=sqlite:///dev.db alembic revision --autogenerate -m "add foo column"
# 3. Review the generated file in alembic/versions/, edit if needed
# 4. Test locally:
DATABASE_URL=sqlite:///dev.db alembic upgrade head
# 5. Commit and push. Deploy auto-runs `upgrade head` on prod.
```

---

## 10. Contact map

- Railway support: dashboard → support
- Cloudflare R2: dashboard → support
- Stripe: dashboard
- Vertex AI: GCP Console → Support → Cases

For platform issues, open the relevant vendor support ticket BEFORE
escalating to engineering — most prod incidents during launch are
infrastructure, not code.
