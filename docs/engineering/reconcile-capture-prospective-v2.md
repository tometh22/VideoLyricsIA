# Prospective association capture: second, explicitly bounded selection

This instrumentation does not enable the frozen 1cc550b5 reconciler or run a
recognizer. PR #1334 remains a separate quality-gated change. The original
`reconcile-ownership-prospective-v1` ledger and its six outcomes are preserved.
Four were technical smokes; two selected jobs lack their first execution trace
in the evidence examined. This selection does not reproduce those incidents.

## Selection fixed before outputs

Cohort: `reconcile-ownership-prospective-v2`. First six distinct **first machine
transcriptions** from the two explicitly allowlisted campaigns, admitted before
recognition. Campaigns remain distinct in every report:

- `ba3318bdfffe`: Argentina campaign.
- `eed1094328c5`: Chile campaign.

Admission uses server-owned campaign/item membership, batch workload, and the
current bound transcription outbox attempt. No approved job, existing editor
document (including active or revision-zero documents), or prior segment output
is eligible. Missing/ambiguous metadata is excluded before reservation. Ordinary
interactive uploads and technical smokes have no eligible campaign membership.
There is no filename, duration, model score, or expected-winner heuristic.
Existing queued uploads can qualify; creating a new upload is not required.
This task never submits or reprocesses a job. Unknown future job IDs stay unknown.

Keep every admitted outcome, including failure or incomplete captures. Do not
replace a failed admission, reopen the old cohort, or replenish a missing stratum.
Check the existing exposure register by audio identity before comparing variants.
Previously inspected audio remains development evidence even under a new job ID.

Predeclared strata, using the captured route and baseline association only:

1. Reconcile accepted with a shifted/skipped word occurrence: activation case.
2. Reconcile accepted with correct word ownership: matched control.
3. Reconcile declined or bypassed: route control.

Compare all six complete documents. Do not select only shortened endings; preserve
controls with unchanged and extended ends. Report missing strata explicitly.

## Activation and retention

Only the coordinated staging release owner enables these settings after clean CI
and served-commit verification. Setting the cohort name alone does not reset v1.
The eligibility policy is opt-in; unset settings retain the old closed selection.

```
RECONCILE_CAPTURE_ENABLED=1
RECONCILE_CAPTURE_COHORT=reconcile-ownership-prospective-v2
RECONCILE_CAPTURE_POLICY=fresh-campaign-v1
RECONCILE_CAPTURE_CAMPAIGN_IDS=ba3318bdfffe,eed1094328c5
RECONCILE_CAPTURE_WINDOW_SECONDS=604800
RECONCILE_CAPTURE_UNTIL=
```

The seven-day admission window starts atomically at the **first eligible
admission**, not deployment. Maximum six jobs, 64 stages and 4 MiB per checkpoint.
The small admission ledger does not expire, so a closed selection cannot restart.
Each private checkpoint expires seven days after its last successful write;
this retention is separate from the admission deadline. Maximum six checkpoint
keys (24 MiB payload ceiling) on the existing staging Redis, no new infrastructure.

Capture records code/config/audio identities at admission, processed inputs
immediately before reconcile, and the final machine result through the existing
machine-evidence builder. The private checkpoint exists independently of editor
persistence. First-attempt identity fences later retries; a complete checkpoint
cannot be replaced. An interrupted run may retain only an incomplete checkpoint.
Unavailable Redis, failed comparisons, capacity limits, and lost checkpoints are
explicit limitations, never evidence of successful repair. Transcription continues.
There is no public endpoint or change to a human document.

## Export and evaluation

`scripts/export_reconcile_checkpoints.py --cohort <explicit cohort> --output <new file>`
reads only ledger members, validates trace/attempt hashes, and creates a private
file exclusively. The existing Railway SSH export transport can import `collect`
without writing anything on the server. No database scan is required for export.

1. Preserve the complete envelope and ledger; record exposure and strata.
2. Extract each complete `checkpoint.trace` into a new private file. Partial
   checkpoints cannot satisfy the final-output gate.
3. Use the existing `scripts/replay_reconcile_capture.py` and the exact captured
   backend/configuration. Baseline must reproduce that execution first; a mismatch
   blocks the candidate. Do not downgrade this to a text-only comparison.
4. Compare the checksum-pinned 1cc550b5 candidate on precisely the same input,
   including all downstream document transformations. Explain every word
   membership, occurrence, text, omission, and time difference. Missing opaque
   downstream inputs remain a blocker for an editor-quality claim.
5. Prepare listening clips only for changed ends needing human judgment. An
   editorial endpoint is not automatically a phonetic label. E1 stays separate.

Success requires an activated real case, exact baseline reproduction, demonstrable
ownership repair reaching the final document, no unexplained lexical/occurrence
regression, and unchanged matched/bypass controls. An absent stratum is not a pass.
No threshold tuning, paid call, or new model experiment is part of this selection.
