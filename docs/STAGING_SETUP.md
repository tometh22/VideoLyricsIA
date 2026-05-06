# Staging Setup — VideoLyricsIA / GenLy

A production-isolated staging environment so we can iterate on code,
schema, prompts, and prices without ever touching the UMG production
data or Stripe live charges.

This is a one-time setup. ~45-60 min of cloud-console clicks.

---

## Architecture

```
                            ┌──────────────────────────┐
                            │  PRODUCTION (untouched)  │
                            │  app.genly.pro           │
                            │  api.genly.pro           │
                            │  Railway: prod services  │
                            │  R2: videolyricsia       │
                            │  Stripe: live keys       │
                            └──────────────────────────┘
                                       ↑ main branch
                                       │
                  git push origin main │
─────────────────────────────────────────────────────────
                  git push origin staging
                                       │
                                       ↓ staging branch
                            ┌──────────────────────────┐
                            │  STAGING (this doc)      │
                            │  staging.app.genly.pro   │
                            │  api-staging.genly.pro   │
                            │  Railway: staging svcs   │
                            │  R2: videolyricsia-stg   │
                            │  Stripe: TEST keys       │
                            └──────────────────────────┘
```

Branch model:
- `main` → production. Pushes auto-deploy via Railway + Vercel.
- `staging` → staging. Same auto-deploy, separate services.
- Feature branches → CI runs, no deploy.

---

## What's already in code (you don't need to do this)

- Backend reads `ENVIRONMENT` env var (`production` | `staging` | `dev`)
  and tags Sentry events with it.
- `/health` returns `env: <environment>` so smoke tests can sanity-check.
- Email sending is gated on non-prod: every outbound message is either
  redirected to `EMAIL_STAGING_REDIRECT` or dropped, and the subject is
  prefixed with `[STAGING]`. Real customers will never receive staging
  mail by accident.
- Frontend reads `VITE_APP_ENV` (or auto-detects via hostname) and
  shows a yellow `STAGING` pill in the sidebar plus stamps the page
  title `[STAGING] GenLy`.
- CI runs on `main` and `staging` branches.

---

## Step 1 — Cloudflare R2 (5 min)

1. Console → R2 → **Create bucket** named `videolyricsia-staging`.
2. Reuse the existing R2 API token — it has account-wide access.
3. Note the bucket name; we'll plug it into Railway later.

Cost: pay-per-use, ~$0/mo at staging volume.

---

## Step 2 — Railway staging services (15 min)

Two options:

### Option A (recommended): same project, separate environment

1. Open the existing Railway project.
2. Top-right → **Environment** dropdown → **+ New environment** → name
   it `staging`.
3. Inside `staging`, add four services with the same source repo:
   - `api-staging` (root: `lyricgen`, start: `bash start.sh`)
   - `worker-staging` (root: `lyricgen`, start: `python backend/worker.py`)
   - `postgres-staging` (Railway's Postgres template)
   - `redis-staging` (Railway's Redis template)
4. For both `api-staging` and `worker-staging` services → **Settings** →
   **Source** → **Branch** = `staging`.

### Option B: separate Railway project

If you prefer total isolation (separate billing line), create a new
project `VideoLyricsIA-staging` and replicate the four services there.
Slightly higher cost, identical from a code perspective.

### Env vars to set on `api-staging` and `worker-staging`

```
ENVIRONMENT=staging
DATABASE_URL=<from postgres-staging>
REDIS_URL=<from redis-staging>

JWT_SECRET=<openssl rand -base64 32>            # NEW value, not prod's
ADMIN_PASSWORD=<a different password than prod>

# CORS — point at the staging frontend hostname
CORS_ORIGINS=https://staging.app.genly.pro

# Cloudflare R2 — separate bucket
R2_BUCKET=videolyricsia-staging
R2_ACCOUNT_ID=<same as prod>
R2_ACCESS_KEY_ID=<same as prod>
R2_SECRET_ACCESS_KEY=<same as prod>

# Stripe — TEST mode (sk_test_..., whsec_..., price_test_...)
STRIPE_SECRET_KEY=sk_test_...
STRIPE_WEBHOOK_SECRET=whsec_test_...
STRIPE_PRICE_100=price_test_...
STRIPE_PRICE_250=price_test_...
STRIPE_PRICE_500=price_test_...
STRIPE_PRICE_1000=price_test_...

# Email gate — redirect everything to your inbox so staging never
# emails a real customer. Setting empty drops all outbound mail.
EMAIL_STAGING_REDIRECT=tomas@epical.digital
EMAIL_STAGING_ALLOWLIST=                          # rare, leave empty

# OpenAI / Vertex / Gemini — same keys as prod is fine; usage on
# staging is small. Optional: rotate to a separate billing project.
OPENAI_API_KEY=<same as prod or a new one>
VERTEX_PROJECT=<same as prod>
VERTEX_LOCATION=us-central1
GOOGLE_APPLICATION_CREDENTIALS=/app/backend/vertex_credentials.json

# Sentry — set SENTRY_ENV=staging if you want a separate Sentry env.
SENTRY_DSN=<same as prod>

# Frontend public URL (used in email links)
FRONTEND_URL=https://staging.app.genly.pro
```

Cost estimate: ~$25-35/mo (api + worker + postgres + redis at the
hobby tier). Negligible vs. the UMG MRR.

---

## Step 3 — Railway custom domain for the API (3 min)

1. `api-staging` service → **Settings** → **Networking** → **Custom
   domain** → add `api-staging.genly.pro`.
2. Railway gives you a CNAME target. Add the CNAME in Cloudflare DNS:
   - Type: `CNAME`
   - Name: `api-staging`
   - Target: `<the Railway target>`
   - Proxy: **DNS only** (grey cloud) — Railway handles TLS itself
3. Wait ~2 min for the cert to issue. `curl https://api-staging.genly.pro/health`
   should return `{"status":"ok","env":"staging",...}`.

---

## Step 4 — Vercel staging deployment (5 min)

In the existing Vercel project for the frontend:

1. **Settings** → **Git** → **Production Branch** = `main` (already).
2. **Settings** → **Domains** → add `staging.app.genly.pro` → assign
   it to **the `staging` branch** (Vercel UI: "Branch settings").
3. **Settings** → **Environment Variables** → make sure both env
   columns are filled:

| Variable          | Production            | Preview (= staging branch) |
|-------------------|----------------------|----------------------------|
| `VITE_API_URL`    | `https://api.genly.pro` | `https://api-staging.genly.pro` |
| `VITE_APP_ENV`    | `production`          | `staging`                  |

Setting `VITE_APP_ENV=staging` explicitly is belt-and-braces — the
runtime hostname heuristic in `src/env.js` would also catch it, but
explicit is safer.

DNS for `staging.app.genly.pro` is just the standard Vercel CNAME
record (Vercel walks you through it).

---

## Step 5 — Stripe TEST mode (3 min)

1. Stripe dashboard → toggle **Test mode** (top-right).
2. **Developers → API keys** → copy `sk_test_...` and `pk_test_...`
   (not used by backend yet but noted).
3. **Products** → recreate the four price points in test mode:
   - Plan 100 → `price_test_...`
   - Plan 250 → ...
   - etc.
4. **Webhooks** → add endpoint `https://api-staging.genly.pro/billing/webhook`
   → copy `whsec_test_...`.
5. Plug all of those into Railway staging env vars (Step 2).

You can test checkout with Stripe's test card `4242 4242 4242 4242`,
any future expiry, any CVC.

---

## Step 6 — Boot + first-deploy smoke (5 min)

After Railway finishes the first build of `staging`:

```bash
# 1. API health says env=staging
curl https://api-staging.genly.pro/health
# expect: {"status":"ok","env":"staging","redis":"up","r2":"configured",...}

# 2. Open https://staging.app.genly.pro
#    - Sidebar shows yellow "STAGING" pill
#    - Browser tab title is "[STAGING] GenLy"
#    - Login with the staging admin password
#    - Upload a tiny MP3, watch it process end-to-end

# 3. Trigger a fake "password reset" → confirm the email lands in
#    EMAIL_STAGING_REDIRECT inbox with subject "[STAGING] ..." and the
#    body's "to" matches what you typed
```

If any of those fail, troubleshoot in this order:
1. Railway service logs (`api-staging` and `worker-staging` tabs).
2. Check the env var values — Railway has a "View" button.
3. `curl https://api-staging.genly.pro/health` again.
4. CORS errors in browser console → confirm `CORS_ORIGINS` matches
   exactly (no trailing slash, scheme correct).

---

## Daily workflow once staging is live

```bash
# Develop on a feature branch (CI runs, no deploy):
git checkout -b feat/whatever
git push -u origin feat/whatever

# Merge into staging to deploy to staging:
git checkout staging && git merge feat/whatever && git push origin staging
# → Vercel deploys staging.app.genly.pro
# → Railway deploys api-staging + worker-staging

# After validating on staging, fast-forward staging into main:
git checkout main && git merge staging && git push origin main
# → Vercel deploys app.genly.pro
# → Railway deploys api + worker (production)
```

UMG never sees anything until the merge to `main`.

---

## What does NOT change

- Production Railway services, R2 bucket, Stripe live keys, DNS records,
  custom domains.
- The `main` branch's CI / deploy pipeline is identical.
- Existing UMG users and jobs.
