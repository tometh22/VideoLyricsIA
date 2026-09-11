# Prospective word-association validation

The behavior candidate stays frozen at `1cc550b5913f27a7c44cec33e0ad7401583a4c66` (module SHA256 `cb5a509292b5bffa1d94218b7fdda9cef856e942651da9c0c1e0d3edc4f87b60`). This release does **not** contain or enable its behavior change. E1 is a separate unresolved endpoint issue; fixing it is not a prerequisite for this evaluation.

## Capture contract

`reconcile_capture.begin` reserves a job **before recognition**. It requires all three settings below, `ENVIRONMENT=staging`, an existing Redis connection, and a deadline no more than seven days away. Otherwise it performs no capture; inference continues normally.

- `RECONCILE_CAPTURE_ENABLED=1`
- `RECONCILE_CAPTURE_COHORT=reconcile-ownership-prospective-v1` (do not rotate to expand the cohort)
- `RECONCILE_CAPTURE_UNTIL=<explicit UTC ISO deadline>`

Redis atomically admits the first **six distinct ordinary jobs**, across workers, for this cohort. Retries consume no additional admissions and do not overwrite a capture. Failed jobs can leave fewer than six durable captures; do not buy replacements or automatically extend the cohort. Every payload is limited to 4 MiB/64 stages; overflow or serialization problems mark it incomplete rather than silently making truncated data replayable. No provider call, queue job or DB connection is created by capture. The only Redis write is its expiring reservation set on the existing service.

The four `reconcile` call sites in `_run_transcription_for_job` preserve:

1. Exact processed `wx_segs`, exact canonical text, route label and kwargs immediately before the one existing call.
2. Its returned output, including `None`, or an exception marker.
3. On the primary catalogue route: output of the existing gap-cluster pass, cleanup input/kwargs and output.
4. At the common emitter: input and annotation arguments, annotated/deduplicated/word-normalized/split/beat/repetition/lead-hold/final presentation output. Beat detection is observed once; its grid and window are stored without rerunning detection.
5. The normal async worker's post-pass outputs and the final machine document before private keys are removed. Legacy sync routes have the emitter/final snapshots but not per-worker post-pass checkpoints; that scope is explicit.

Uploaded, alignment and presentation audio identities are file SHA256 plus byte length, without local paths. Release identity, source-file SHA256s, safe allowlisted environment configuration, and route request context travel with the capture. No credentials or evidence signing keys are serialized.

Persistence uses `machine_evidence.capture.reconcile_stages`, inside the existing immutable pre-human snapshot and its existing evidence hash. It adds no schema migration, recognition attempt or approval requirement. The transport key is removed before normal responses. Disabling capture does not change word/timing behavior. Capture failure does not change transcription success.

## Selection fixed before comparing variants

All six admitted jobs belong to the cohort regardless of outcome. Export/record their job IDs, audio identities, ordinals, release and prior-exposure status **before** running the candidate. Use the existing exposure register; do not reopen the 75-song census. A new job ID with previously inspected audio is still exposed.

Report the following strata; do not cherry-pick a winner or replace missing strata:

- Reconciliation accepted with reanchoring or skipped reference lines (potential ownership defect).
- Reconciliation accepted without ownership drift (matched control).
- Reconciliation declined / bypassed (route control).

Compare every complete eligible document in each stratum. If six ordinary jobs do not provide both activation and control cases, report exactly which stratum remains absent. No extra ASR execution is authorized by this protocol.

## Offline baseline first

Export the private evidence for one newly captured job read-only. Use the **exact captured backend checkout** and its evidence-signing environment, without exporting secrets. The script rejects source mismatches and disables outbound sockets. Signing-key absence/rotation will cause an exact annotation mismatch, which is a replay blockage rather than a transcription regression.

Extract only the frozen candidate module to an isolated local path, then run:

```sh
git show 1cc550b5913f27a7c44cec33e0ad7401583a4c66:lyricgen/backend/whisperx_reconcile.py > /private/tmp/reconcile-candidate-1cc550b5.py
python lyricgen/backend/scripts/replay_reconcile_capture.py \
  --capture /private/tmp/prospective-machine-evidence.json \
  --backend lyricgen/backend \
  --candidate-file /private/tmp/reconcile-candidate-1cc550b5.py \
  --output /private/tmp/prospective-comparison.json
```

The input is deep-copied identically for each arm. Before invoking the candidate, the runner requires exact baseline reconciliation, cleanup, annotation and full presentation equality. It never substitutes an opaque changed gap-cluster result into the candidate. A route change or un-replayed transition is explicitly blocked. All complete arm documents, every changed field, and links from words to original input occurrence indices are retained. Repeated identical timestamps remain ambiguous instead of being paired arbitrarily.

This runner verifies the reconcile-to-presentation chain. It **does not** treat unchanged baseline downstream passes as proof that they would ignore a changed candidate. It preserves every recorded downstream document/transition and reports whether final output differs from presentation. Complete the required downstream replay from those checkpoints before calling the usual editor route repaired; do not count a presentation-stage improvement, alert or abstention as final repair.

## Prospective success criteria

- Exact baseline reproduction before scoring the candidate.
- Every changed word bag maps to the helper's selected input occurrences; no invented association when multiple input indices match.
- No new omitted/added/reordered phrase or wrong repetition association in complete documents. Layout changes stay unscored pending explicit association.
- End changes must be assessed against listening on **only the changed fragments**, with separate voice and display marks. Include retained/extended controls; do not optimize for shortening. Retain all observed regressions.
- At least one confirmed ownership repair reaches the ordinary editor route, with no unacceptable new content/occurrence/timing regression. Synthetic tests prove instrumentation and the defect mechanism only.
- No tuning of candidate or thresholds. No claim that prospective success reproduces the old historical incident.

## Release ownership and pending execution

Staging instrumentation is coordinated after Worcester's PR #1320 / 1.1.17 release. Riyadh owns this separate 1.1.18 integration only once that window is explicitly handed over. Version files move together and clean-checkout CI must pass. Verify the deployed worker code/hook/hash and capture settings without submitting audio. Then leave the bounded mechanism ready.

No ordinary transcription is currently scheduled by the release owner. The concrete missing execution is the next already-authorized ordinary staging upload/transcription, followed by the admitted controls within the same six-job cohort. Do not wait actively, submit a synthetic ASR job, touch an approved song/session, or purchase a run to fill it.
