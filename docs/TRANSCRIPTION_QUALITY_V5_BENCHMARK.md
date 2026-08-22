# Transcription Quality v5 Benchmark

This benchmark is the release gate for transcription and lyric-timing changes. It is deliberately fail-closed: no score is produced when provenance, hashes, human review, split isolation, or pipeline identity cannot be proved.

## Commands

From `lyricgen/backend`:

```bash
python scripts/benchmark_v5.py validate --manifest benchmark/v5/manifest.json
python scripts/benchmark_v5.py score --manifest benchmark/v5/manifest.json
python scripts/benchmark_v5.py score \
  --manifest benchmark/v5/manifest.json \
  --output benchmark/v5/report.json
python scripts/benchmark_v5.py gate \
  --manifest benchmark/v5/manifest.json \
  --output benchmark/v5/release-report.json

# Run only in the trusted benchmark environment after human review labels.
python scripts/export_shadow_ledger.py \
  --candidate-release "$RELEASE" \
  --candidate-config benchmark/v5/configs/candidate.json \
  --reviews benchmark/v5/shadow-reviews.json \
  --output benchmark/v5/shadow-ledger.json

# Export server-clocked operator effort (private key in trusted exporter only).
python scripts/export_operator_evidence.py \
  --case-id CASE --system candidate --job-id JOB --revision REVISION \
  --snapshot-sha256 SHA --pipeline-release "$RELEASE" \
  --config-sha256 CONFIG_SHA --pipeline-config-fingerprint FINGERPRINT \
  --scored-output benchmark/v5/cases/CASE/candidate.json \
  --output benchmark/v5/operator-evidence/CASE.json

# Sign a reconciliation already joined to provider receipts/invoice snapshots.
python scripts/export_cost_evidence.py \
  --input benchmark/v5/finops/CASE-reconciliation.json \
  --output benchmark/v5/cost-evidence/CASE.json
```

All commands exit nonzero for an invalid corpus. `score` always runs the complete validator first and never emits a partial report.
`gate` additionally exits `2` while any release criterion is missing or below
target. A tiny fixture can validate and score, but can never masquerade as a
release corpus: its report remains `release_gate.decision=NO_GO`.

## Manifest contract

The manifest has `schema_version: 5`, a stable `benchmark_id`, exactly three system pins (`current`, `candidate`, `rotor`), and one or more entries. Every referenced artifact uses a relative path and a lowercase SHA-256 hash. Absolute paths and paths outside the manifest directory are rejected.

Each system pin contains:

```json
{
  "release": "git-sha-or-provider-version",
  "config": {"path": "configs/candidate.json", "sha256": "..."},
  "render": false
}
```

Every case must provide:

- `case_id` and split `dev` or `holdout`.
- Non-empty artist, song, and master identities plus the audio SHA-256.
- The hashed audio artifact.
- Exactly two hashed and Ed25519-attested annotation files from two distinct authenticated annotators.
- One hashed and attested adjudication file from a third authenticated person. It must reference exactly both annotation hashes.
- One hashed gold file with `verified: true`, signed by that adjudicator and bound to the adjudication hash. Gold segments must exactly equal adjudicated segments.
- Hashed outputs for exactly `current`, `candidate`, and `rotor`.
- For release gating, `category` (`live`, `studio`, or `adversarial`), optional
  `tags` such as `repetition`, `adlib`, `crowd`, or `chorus`, and exactly one
  `regression_fixture: "los_pericos"` case.

Artist, song, master, and audio identities are independently checked across the dev/holdout boundary. Any repeated identity on both sides rejects the full benchmark.

Annotation, adjudication, gold, and output files use ordered `segments`:

```json
{
  "start": 60.85,
  "end": 63.77,
  "text": "Real, uoh uoh",
  "event_type": "mixed"
}
```

`event_type` is one of `lexical`, `vocalization`, or `mixed`. Times must be finite, non-negative, strictly increasing, non-overlapping, and have `end > start`.

Every output bundle also declares `schema_version`, `case_id`, `system`, `release`, `config_sha256`, `render`, `operator_review_minutes`, `cost_usd`, and `segments`. Release/config must equal its corpus-wide system pin and `render` must be exactly `false`. Operational time and cost keys are mandatory but may explicitly be `null`; this makes missing coverage visible without fabricating values.

A non-null operator time additionally requires a hashed
`server-editor-session-evidence-v1` artifact. Its active minutes must equal the
output value and it must reference the server-side product-event IDs used to
derive the session. The official exporter derives time exclusively from
bounded gaps between server-timestamped heartbeats bound to one operator,
session, revision and lyric snapshot; browser-reported minutes are ignored.
A non-null cost additionally requires a hashed
`reconciled-cost-ledger-v1` artifact with provider/SKU/request line items,
complete allocation, reconciliation status, and a line-item sum exactly equal
to `cost_usd`. Both artifacts require Ed25519 receipts from runner-approved
operator-evidence/FinOps keys and bind case, audio, release and configuration.
Missing client-independent time evidence or incomplete costs
(including Demucs/compute/storage and failed billable requests) never count
toward release coverage.

## Metric definitions

- **WER/CER:** micro-averaged Levenshtein error over Unicode-NFKC, case-folded text. Punctuation is ignored; accents remain significant. CER excludes spaces.
- **Monotonic alignment:** one-to-one dynamic-programming event alignment. Matches can be established by temporal overlap, sufficient text similarity, or a nearby vocalization. Indices can never move backward and split/merge mistakes remain visible.
- **Event count:** gold count, predicted count, absolute error, and monotonic matched-event precision/recall/F1. The same matching counters are also exposed under `alignment` for auditability.
- **Vocalization:** precision/recall/F1 where `vocalization` and `mixed` are positive classes. Unmatched positives count as false positives or false negatives.
- **Boundaries:** onset/end MAE and conservative nearest-rank p90 over aligned events, plus onset, end, and joint recall at 100 ms and 200 ms.
- **Operator effort:** coverage and p50/p90 active review minutes. Median is used for p50; p90 uses conservative nearest rank.
- **Cost:** coverage, total, and mean USD per system. Missing values lower coverage and never become zero-cost observations.

The JSON report contains metrics per case and micro-aggregates for `all`, `dev`, and `holdout`. ROTOR is a comparator, never ground truth.

## Dataset and release policy

The release corpus should contain 50 songs: 20 live, 20 studio, and 10 adversarial, including repetition/adlibs and crowd or backing-vocal cases. Use 30 cases for dev/calibration and 20 untouched holdout cases. Freeze and hash every artifact before a comparison run.

The Los Pericos regression requires six events in the 60.85–83.27 second window: four `Real` plus vocalization events, `¡no!`, and the final sustained `¡noooo!`. It is a regression fixture only; production code must never branch on artist, song, ID, or literal lyric text.

Do not render video during benchmark generation. A candidate proceeds to shadow only after satisfying the documented quality targets, including event-count F1, vocalization recall, boundary p90, no material WER regression, and operator p50/p90. Costs must have sufficient coverage before claiming savings.

The contrastive CTC calibration is a separate hash-pinned artifact. It is
accepted only when it names a non-empty calibration ID, the exact 40-hex model
revision and vocabulary identity, four finite/ranged stem+mix thresholds, the
current pipeline release/configuration fingerprint, `release_gate_decision:
"GO"`, and the 64-hex benchmark-manifest and release-report hashes. A moving
model reference, malformed threshold, changed runtime setting, missing report,
or revoked report causes immediate abstention.

The runtime release calibration additionally requires an Ed25519-signed GO
report produced by `benchmark_v5.py gate`, every scorer check present and
true, and the exact benchmark manifest available at
`TRANSCRIPTION_QUALITY_BENCHMARK_MANIFEST_PATH`. A minimal self-authored GO
JSON cannot activate approval.

The top-level `shadow_evaluation` supplies a hashed `ledger` artifact. Scalar
counters are ignored: the gate derives volume, coverage, duration, correctness,
and catastrophic approvals from the individual timestamped rows. Every row is
bound to the candidate release/configuration and the complete ledger carries
an HMAC-SHA256 receipt generated by the trusted backend exporter. The gate
verifies its Ed25519 receipt with `BENCHMARK_SHADOW_PUBLIC_KEYS`; the private
key exists only in the trusted exporter. Missing or untrusted keys,
unreviewed automatic approvals, duplicate decisions, row tampering, a stale
candidate binding, or fewer than 30 elapsed days are all NO-GO.

Automatic precision is evaluated using the one-sided 95% Wilson lower bound,
which must be at least 99%; a perfect but tiny sample therefore cannot approve
a rollout. The operational backend records append-only, privacy-safe
`transcription_quality_shadow_decision` events (hashes/counters only), while
`editor_approved` records active correction time and edit counts. An
independent reviewer must still label every would-approve row before the
ledger can satisfy the release gate. Review files also require an Ed25519
receipt from the authenticated review service and bind each label to the exact
snapshot shown to the reviewer. Cost approval uses
paired per-song candidate-minus-current deltas and requires the upper bound of
the 95% confidence interval to be below zero.
