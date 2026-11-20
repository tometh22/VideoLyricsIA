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

## CI execution

Backend tests run in four file-based partitions on separate runners, each with
its own PostgreSQL service. The required `backend` check verifies that every
partition succeeded and that their manifests cover the exact collected test
inventory once, with no missing, duplicated or foreign tests. Existing marker
exclusions and the infrastructure-skip audit remain in force. Real MoviePy/libass
and the dependency vulnerability gate run on partition 0 and remain blocking.

Docker image builds run alongside the test jobs. `ci-gate` still requires the
backend, frontend, real editor collaboration, Sentinel and both image builds.
Parallel execution does not change the release or staging verification gates.
Shard inventories and test output are retained as CI artifacts for seven days.
Compare workflow job timings to measure the critical path and diagnose failures.

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
- **One runtime deployment cohort** — API and all five worker services share watch patterns. Frontend-only releases must deploy the workers too; the quality CAS token includes the Git SHA.
- **No native canary / percentage rollout** — staging soak is the substitute. For true canary/blue-green/SLO-driven auto-rollback you'd need a traffic proxy or a flag service (LaunchDarkly/Flagsmith); that's a separate investment, relevant around the ~50-user scale inflection.

### SHA-bound runtime and watch-pattern contract

`queue_jobs._transcription_quality_runtime_token()` binds quality work to the
release SHA, policy, pipeline configuration and calibration. A worker with a
different token discards the work before taking its snapshot. Equal backend code
fingerprints alone therefore do not make different release SHAs compatible.

The API, Worker, ShortWorker, BatchWorker, BatchShortWorker and quality-worker
share the same watch paths. They cover root `/VERSION`, frontend, backend,
assets, both Dockerfiles and the six runtime-service TOML files. This makes a
frontend-only release select the complete cohort. Frontend tests or package
changes without a version bump also select everyone: they used to deploy only
the API. A version-only release or a change to one cohort config also selects all
six. Normal application releases still update VERSION/package/changelog together.

Paths are relative to the repository root even though the build root is
`/lyricgen`; see [Railway watch-path semantics](https://docs.railway.com/builds/build-configuration#configure-watch-paths).
Docs, CI-only files, and independent cost-collector/R2 service configs remain
outside these triggers. Do not replace this list with `railway/**` or a global
wildcard that restarts the runtime fleet for unrelated maintenance.

The contract runs in the existing fast CI check:

```sh
python3.11 -m unittest discover -s ci/tests -p test_release_watch_contract.py -v
```

Tests parse the real TOML and use Git's gitignore matcher against changed-path
scenarios; no deployment, database, provider or queue access is involved. The
six lists must remain identical, so adding a new API trigger cannot silently
leave workers behind.

Before publishing, verify that each staging service is actually linked to its
matching `/railway/<service>.toml` and compare its effective watch patterns with
the repository. A direct read of `ServiceInstance` on 2026-11-20 at 15:34 UTC
confirmed a blocking dependency on staging release `4d1d0adb`: BatchWorker and
BatchShortWorker both have `railwayConfigFile=null`. Their `resolvedFileConfig`
for that release also has no config file or file watch patterns. The other four
services have their expected configured and resolved paths. All six effective
watch lists match the old repository patterns, but matching values alone do not
establish that the two batch TOMLs are read.

The release owner must coordinate linking BatchWorker to
`/railway/batch-worker.toml` and BatchShortWorker to
`/railway/batch-short-worker.toml`, with a review of the complete file settings
before applying them. Merely merging these file changes cannot activate the two
unlinked configs. This PR does not change Railway settings and is blocked for
publication until that dependency has an authorized activation plan. Recheck all
six configured paths, resolved file manifests and effective watch lists during
the rollout; repository tests cannot attest to provider-side binding.

This intentionally increases worker rollouts for frontend-only changes. Preserve
all existing queues, replicas and 1200-second draining settings, schedule one
publication at a time, and let in-flight work finish. Watch-pattern selection is
not proof of a completed rollout: before releasing the turn, verify every
service's actual commit, healthy queue/service coverage, config/protocol parity
and `quality_gates.fleet_runtime_token_match=true`. Do not accept a partial
`SKIPPED` rollout on backend fingerprint equality alone. Stop and coordinate
recovery if source identity or runtime tokens disagree; never hide the guard,
change flags to equalize tokens, or publish a feature checkout manually.

Railway's [Config-as-Code reference](https://docs.railway.com/config-as-code/reference)
(checked 2026-11-20) announces legacy TOML support through 2026-12-01. This patch repairs the existing
files; migrating the same contract to Infrastructure as Code is separate,
coordinated work before that deadline, not an infrastructure action in this PR.

### Veo cost-control rollout (schema + locking protocol)

The per-song ceiling changes both the database schema and the delete/submit
locking protocol. It must not ship as one ordinary rolling deploy:

1. Merge/deploy the additive schema release first (#1084). Wait until its API
   migration logs show Alembic at head and `/health/ready` reports one coherent
   release. Do not start #1085 while #1084 is still rolling.
2. Keep `VEO_MAX_CALLS_PER_SONG=0` and `VEO_BUDGET_TENANTS=` while deploying
   #1085. At those values the new ceiling/reservation protocol is not entered.
3. Before enabling the control, pause submissions through
   `PUT /admin/ops/submissions`, wait for `/admin/queue` to be empty, and do not
   delete jobs. Restart API, Worker and ShortWorker with the same variables;
   wait for `/health/ready` to confirm that no old replica remains.
4. Enable one tenant only, for example:

   ```env
   VEO_MAX_CALLS_PER_SONG=10
   VEO_BUDGET_TENANTS=universal_argentina
   ```

   Audit the previous 30 days of incomplete/legacy provenance before this
   flip. Keep submissions paused until every service has the same effective
   configuration, then reopen and run one canary render.
5. Widen `VEO_BUDGET_TENANTS` only after the canary. `*` means all tenants;
   an empty value is the fail-safe rollback and the cap remains zero by
   default in code.

---

## Sequencing the stability tiers

Ship in risk order, **soak between each**, never two at once:

1. **Tier 1** (timeouts, flag-gated) → flip flags in staging → prod. Lowest risk.
2. **Tier 2** (async→def, logic-identical) → soak + **load-test** (`/health/live` latency must stay flat under concurrent DB-touching load) → low-traffic prod window.
3. **Tier 3** (worker segmentation, additive infra) → new short-queue worker service at low scale → ramp.

Each tier = its own branch off clean `origin/staging` → draft PR → soak → promote.
