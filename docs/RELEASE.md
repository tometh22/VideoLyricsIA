# Release runbook — shipping to prod without breaking it

The repeatable process for every backend change. Core principle: **separate
deploy from release** — ship small, reversible steps, **one at a time**, soak in
staging, observe, and have the rollback written *before* you push.

Topology: feature branch → CI (`backend` + `frontend` required) → PR → **staging**
(soak) → promote to **main** (prod). `main` and `staging` are protected (PR only).
Railway deploys `staging` on push (~2 min) and `main` after CI; Railway build is
slow (~20–30 min).

---

## The path every change takes

```
branch (ONE focused change) → CI green + `make check` → PR review
   → merge to STAGING → soak + observe → (perf changes: load-test)
   → promote STAGING → main (PROD) → watch 30–60 min → OK / rollback
```

One change per PR. One deploy at a time — **never a burst** (a 2nd SIGTERM mid-drain
can kill an in-flight render). Never ship two risky changes together.

---

## Pre-deploy checklist

- [ ] CI green; `make check` passes locally (pre-push hook enforces it)
- [ ] PR reviewed (second pair of eyes, or adversarial review for risky diffs)
- [ ] **Rollback written**: the exact command — `git revert <sha>` / flip env flag / scale service to 0
- [ ] Diff is logic-identical OR behind a default-safe flag (see risk classes below)
- [ ] **Idle window confirmed**: no UMG/enterprise render in flight (check `/admin/queue`)
- [ ] DB migrations (if any) are backward-compatible (expand-contract; never drop-then-add)

## During deploy

- [ ] Deploy ONE change; wait for it to reach "healthy" before anything else
- [ ] Watch the deploy logs through rollout completion

## Post-deploy — watch for 30–60 min

- [ ] **Error rate** in Sentry — any new spike tied to the deploy?
- [ ] **Latency** p95/p99 on the API
- [ ] **DB pool utilization** (exposed by `/health`) — the signal for event-loop/pool changes
- [ ] **Queue depth** (`/admin/queue`) — the signal for worker changes
- [ ] One real end-to-end render as a smoke test

If any red → execute the written rollback immediately. Don't debug forward in prod.

---

## Risk classes — how each kind of change ships

| Class | Example | Release mechanism | Rollback |
|---|---|---|---|
| **Flag-gated** | new behavior behind `ENV_FLAG` defaulted to current | flip on in staging → soak → flip on in prod | flip flag off (no redeploy) |
| **Logic-identical** | `async def`→`def` (Tier 2) — can't be flagged | tests + staging soak + load-test + low-traffic window | `git revert` (rehearsed) |
| **Additive infra** | new worker service alongside existing (Tier 3) | deploy new service at low scale → ramp | scale new service → 0 |
| **Migration** | schema change | expand → deploy → backfill → contract (separate PRs) | the expand step is reversible; never contract before prod is fully on the new shape |

Prefer flag-gated. Where impossible (logic-identical), the safety net is coverage +
soak + fast revert, so those go out in a **low-traffic window** with eyes on the
dashboards.

---

## Railway-specific gotchas

- **No burst deploys** — kills in-flight renders. One at a time, wait for healthy.
- **Build ~20–30 min** — plan around it; not instant.
- **`RAILWAY_SHUTDOWN_TIMEOUT_SECONDS=1200`** on Worker — leave it; it's what lets a render finish across a deploy. Verify it's still set after any service recreation.
- **`watchPatterns`** keep the Worker from redeploying on frontend-only changes — don't remove.
- **No native canary / percentage rollout** — staging soak is the substitute. For true canary/blue-green/SLO-driven auto-rollback you'd need a traffic proxy or a flag service (LaunchDarkly/Flagsmith); that's a separate investment, relevant around the ~50-user scale inflection.

---

## Sequencing the stability tiers

Ship in risk order, **soak between each**, never two at once:

1. **Tier 1** (timeouts, flag-gated) → flip flags in staging → prod. Lowest risk.
2. **Tier 2** (async→def, logic-identical) → soak + **load-test** (`/health/live` latency must stay flat under concurrent DB-touching load) → low-traffic prod window.
3. **Tier 3** (worker segmentation, additive infra) → new short-queue worker service at low scale → ramp.

Each tier = its own branch off clean `origin/staging` → draft PR → soak → promote.
