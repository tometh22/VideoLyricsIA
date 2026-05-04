# Preflight test suite

Pre-launch verification for production readiness. Each check is small,
self-contained, and emits a structured `CheckResult` with pass/fail/warn
status, human-readable summary, and machine-readable details.

The runner aggregates everything into a Markdown report (for humans) and a
JSON file (for CI / programmatic parsing) under `reports/`. A top-line
**GO / NO-GO / INCONCLUSIVE** verdict is printed based on every P0 check.

## Why this exists

A handful of bug classes are uniquely expensive in this codebase:

- **Silent cap drift.** A refactor that drops `_enforce_daily_volume_cap`
  from one of the upload handlers turns a $1 mistake into a $5k bill.
- **UMG master spec violations.** Wrong codec / fps / pix_fmt and UMG
  rejects the deliverable. Costs an entire generation cycle to fix.
- **Validator regression.** If the content validator becomes too strict,
  legitimate jobs auto-fail; too lax, prohibited content slips through.

These are exactly the failure modes regular unit tests miss because they
require live infra (Veo, R2, Postgres, ffprobe).

## Running

```bash
cd lyricgen/backend
python3 -m scripts.preflight.run
```

Common flags:

```bash
# only the cheap checks (skip the one that hits Veo $$)
python3 -m scripts.preflight.run --skip validator_quality

# only one check
python3 -m scripts.preflight.run --only volume_caps

# verify a UMG master file
python3 -m scripts.preflight.run \
  --only umg_master_conformance \
  --umg-master /path/to/<job_id>_umg_master.mov

# tighter validator audit budget
python3 -m scripts.preflight.run \
  --only validator_quality \
  --validator-prompts 3 \
  --validator-budget 3.0
```

Exit codes: `0` GO, `1` NO-GO, `2` INCONCLUSIVE (every P0 skipped).

## Cost notes

| Check                    | Cost per run                              |
|--------------------------|-------------------------------------------|
| `volume_caps`            | $0 — static analysis + DB read            |
| `umg_master_conformance` | $0 — only ffprobes a file you give it     |
| `validator_quality`      | up to `$validator_budget` (default $5)    |

`validator_quality` hits the R2 cache layer in `pipeline.py`, so re-runs
with the same prompts are free after the first pass.

## Daily smoke (CI)

`scripts/preflight/daily_smoke.py` is a slimmed-down runner that only
exercises the zero-cost checks (`production_health` + `volume_caps`) and
posts an HTML alert to `ALERT_EMAIL` via Resend if anything is red. It
exits non-zero on failure so the calling job is also flagged.

Schedule it from `.github/workflows/daily-smoke.yml` — runs once a day at
09:00 Buenos Aires (12:00 UTC), or on-demand from the Actions tab.

Required GitHub repository secrets:

| Secret               | Purpose                                                      |
|----------------------|--------------------------------------------------------------|
| `PRODUCTION_API_URL` | Base URL of the deployed API (e.g. `https://...railway.app`) |
| `DATABASE_URL`       | Public Postgres URL — used to audit per-user cap overrides   |
| `RESEND_API_KEY`     | Same key the API uses for transactional email                |
| `RESEND_FROM`        | Verified sender (e.g. `noreply@genly.pro`)                   |
| `ALERT_EMAIL`        | Where failure notifications are delivered                    |

Set them in GitHub: **Settings → Secrets and variables → Actions →
New repository secret**.

To pause the daily run, either disable the workflow from the Actions
tab or remove its `schedule:` block.

## Adding a check

1. Create `check_<name>.py` with a class that subclasses `Check`.
2. Set `name`, `description`, `p0` (True only if a fail blocks UMG go-live).
3. Implement `run()` returning `self._passed(...)`, `self._failed(...)`,
   `self._warned(...)`, or `self._skipped(...)`.
4. Register it in `run.py::build_checks`.

Keep each check under one job: one cohesive verification, one summary
sentence. Compose in `run.py` rather than building a 500-line do-everything
class.
