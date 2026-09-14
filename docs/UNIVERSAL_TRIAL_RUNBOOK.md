# Frozen Universal trial — 1.1.18-trial.2

## Isolation and release

Baseline: `45ba210abff758c335b8eef35e1902feb6374f2f`.
PR base: `release/universal-es-trial-20260911`, never `staging` or `main`.
Update VERSION, frontend package.json/package-lock.json and CHANGELOG together.
Merge only after clean-checkout CI succeeds on the exact candidate. Verify the
same release on all three trial services and trial.genly.pro before inviting.

- Railway project: `8b4db4c9-5955-40cf-aa4a-14e2a04996ca`
- Environment: `universal-trial` (`19f9ddfe-a39c-41b4-b467-b259d4fb52bf`)
- Services: `api-universal-trial`, `worker-universal-trial`, `shortworker-universal-trial`
- Frontend: Vercel `genly-universal-trial` (`prj_4RS3NJEacrUqwa5SBiwibmepaCvd`), trial.genly.pro

Do not start/modify shared staging, production, batch, quality or cost workers.
Do not deploy the parent Stockholm placeholder checkout; it is an older version.

## Trial policy

Opt in only the dedicated group with `TRIAL_BILLING_GROUPS=universal-es-trial-24h`
on API and both workers. Keep the same flag on all three. An isolated QA group
may be added during tests; remove it or deactivate its users after verification.
Set `TRIAL_ONLY_MODE=1` on these three services. This is a private environment:
public registration is disabled, non-invited accounts (including old JWT/API keys)
cannot use it, and batch admission remains off. Admin provisioning still works.
Do not enable this private-environment flag on shared staging or production.
No schema migration is needed: dedicated CreditGrant reason `bounded_trial_v1`
and append-only AuditLog reservations are used. Old bonus grants do not activate
this trial or add free-plan credits.

Activation is a separately authorized admin action, **not deployment or login**:
`POST /admin/trials/universal-es-trial-24h/activate` with the normal admin Bearer
session. It grants exactly 9 shared credits for 24 hours from server time. Retrying
the operation returns the same activation, never a fresh clock or extra credits.
Do not call it until Tomi explicitly supplies the activation instruction.

Each new Scenes video reserves 3 credits atomically with its durable generation
publication. Three such videos exhaust the pool; simultaneous users cannot
overspend. Approval does not charge twice. Rejecting/deleting a video cannot
refund already-spent provider cost. Already reserved videos may be edited and
finished before expiry. At expiry mutations stop; existing read/download access
is retained. Work already running is not killed mid-provider-call, but queued
work checks expiry before starting. An exhausted/expired trial never falls back
to the free-plan monthly allowance.

Set `SCENE_REROLL_MAX=3` for the trial. This cap includes failed attempts; support
must investigate provider outages instead of unlimited paid retries. Existing
general edit limit still applies; main-pipeline attempts are capped at 3 per job.
Transcription has a separate shared allowance of 6 attempts, including retries,
so draft creation cannot produce unlimited paid transcriptions. It does not
deduct video credits. No new transcription starts once video credits are exhausted.
Keep REQUIRE_REVIEW=true, max_jobs=1, backlog limits5, daily cap10, AI previews
and batch disabled. A queue is expected with two simultaneous users.

Disable inherited experimental capture (`RECONCILE_CAPTURE_ENABLED=0`) across
the trial fleet; do not change shared staging capture configuration. All other
transcription/timing flags must remain aligned across these three services.

## Acceptance / rollback

Use dedicated QA accounts, never customer credits. Verify pending activation,
idempotent activation, 9-credit boundary under real PostgreSQL contention,
expiry with an already-issued token, worker queue expiry, literal prompt and
contrast/color persistence through reload, render and targeted scene regeneration.
Check a failed/substituted scene cannot be approved, and a repaired scene clears
the gate without changing unrelated clip identities. A provider may still make
artistic mistakes: inspect outputs and do not promise identical characters or
perfect chronology. Validate a representative authorized song before claiming
real music transcription accuracy; synthetic speech is only a control.

Retain previous deployment IDs and frontend URL before promotion. Roll back only
these trial services/frontend, preferably to a policy-capable release. Do not
expose the predecessor snapshot without invitation/clock guards: deactivating the
customer accounts alone does not close its public registration. If that older
code is needed for diagnosis, keep the trial API/workers stopped or inaccessible
until the guards are restored. Never roll back shared production/staging or erase
ledger evidence.
