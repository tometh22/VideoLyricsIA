# Transcription Quality v6 — Runtime Shadow Analysis and Offline Calibration

Quality v6 has two deliberately separate boundaries. The CPU Performance
Graph may run in the isolated quality worker under policy
`lyrics-quality-v6`, initially in shadow/observe mode. The learned XLS-R
phone/event model and its calibration remain offline research artifacts. No
v6 path may modify editor segments automatically, render video, or authorize
a rollout from uncalibrated evidence. Review proposals are independently
kill-switched and fail closed unless their signed dataset and calibration
artifacts satisfy the runtime gate.

## Safety boundary

- Dataset and calibration artifacts require detached Ed25519 attestations from
  separate trusted key allow-lists.
- Missing, untrusted, undersized, expired, leaking or commercially ambiguous
  evidence fails closed.
- An offline calibration always contains `runtime_authorization: false` and
  `automatic_apply_allowed: false`.
- `QUALITY_V6_ANALYSIS_ENABLED=1` enables only the heuristic Performance Graph
  analysis. `QUALITY_V6_PROPOSALS_ENABLED` and `QUALITY_V6_MODEL_ENABLED`
  remain independent and disabled by default.
- The training command uses a local XLS-R directory with
  `local_files_only=True`, `HF_HUB_OFFLINE=1` and
  `TRANSFORMERS_OFFLINE=1`. It cannot download a model.
- Training emits only a signed `trained_uncalibrated` research checkpoint.
  It never describes that checkpoint as calibrated or exported.

## Dataset contract

Build an inventory JSON with `dataset_id`, `contract` and `entries`, then run:

```bash
cd lyricgen/backend

# Curation only. This writes an unsigned draft that consumers reject.
python scripts/build_v6_dataset_manifest.py \
  --inventory /secure/v6/inventory.json \
  --output /secure/v6/manifest-draft.json \
  --draft

# Trusted publication. The private key never belongs in the repository.
export QUALITY_V6_DATASET_PRIVATE_KEY='base64-ed25519-private-key'
export QUALITY_V6_DATASET_KEY_ID='quality-v6-dataset-2026-01'
python scripts/build_v6_dataset_manifest.py \
  --inventory /secure/v6/inventory.json \
  --output /secure/v6/manifest.json
```

The top-level contract fixes the permitted purpose to
`offline_phone_event_training_calibration`, requires tenant opt-in, tenant
isolation of raw content, exclusion of ambiguous rights, a legal review ID and
a revocation process.

Every entry contains:

- A split: `training`, `regression`, `calibration` or `temporal`.
- A category: `live`, `studio` or `adversarial`.
- SHA-256 identities for artist, song, master and audio. Identity reuse across
  splits rejects the complete dataset.
- Private audio URI, content hash and duration.
- Adjudicated hierarchical annotation with at least two annotators, a separate
  adjudicator hash, artifact hash and event count.
- License ID/name/URI, rights basis, evidence hash and explicit grants for
  commercial use, derivatives, model training and global model training.
- Training rows additionally reference a hashed `phone-event-npz-v1` example,
  its 16 kHz sample rate, materialized duration and event count. Training-hour
  and event gates are derived from these examples, not from song metadata.

The minimum adequate corpus is:

| Partition | Requirement |
|---|---:|
| Training | 100 songs, 25 hours, 2,000 events, at least 50% live |
| Regression | exactly 50: 20 live, 20 studio, 10 adversarial |
| Calibration | at least 300 masters |
| Locked temporal evaluation | at least 150 later masters |

These are minimum evidence volumes, not a guarantee of quality. Artist, song,
master and audio hashes must be disjoint across all four partitions.

## Selective calibration contract

`quality_v6_calibration.py` validates three independent conformal components:

- Cardinality: split-conformal singleton set.
- Content: APS/RAPS singleton set.
- Timing: conformalized onset/end intervals no wider than 400/700 ms.

Each component needs at least 300 calibration examples, target and empirical
coverage of at least 99%, the hash of the signed calibration split, and its
declared decision contract. Timing coverage is joint over onset and end. A non-singleton
cardinality or content set always abstains. Invalid, overlapping or overly wide
timing intervals always abstain.

Action precision uses the one-sided 95% Wilson lower bound:

| Action | Minimum reviewed | Required lower bound |
|---|---:|---:|
| Review suggestion | 300 | 95% |
| Reversible timing | 539 | 99.5% |
| Reversible content | 539 | 99.5% |
| Structural insertion/deletion/cardinality | 3,000 | 99.9% |

Any catastrophic outcome closes the corresponding action gate. Evidence is
evaluated by action; success in timing cannot authorize a structural change.
The locked temporal evaluation needs at least 150 cases and zero catastrophic
outcomes.

## XLS-R phone/event training scaffold

First validate or produce a non-executable plan:

```bash
export QUALITY_V6_DATASET_PUBLIC_KEYS='{"quality-v6-dataset-2026-01":"base64-public-key"}'

python scripts/train_phone_event_model.py validate \
  --manifest /secure/v6/manifest.json

python scripts/train_phone_event_model.py plan \
  --manifest /secure/v6/manifest.json \
  --base-model-path /models/xls-r-300m-pinned \
  --base-model-sha256 "$LOCAL_MODEL_DIRECTORY_SHA256" \
  --output /secure/v6/training-plan.json
```

The model has an XLS-R encoder plus phone-CTC, event, tri-state boundary and
onset/offset heads. The declared event vocabulary includes sung lead, sung
crowd, speech, nonlexical material, crowd noise and unknown. Boundary labels
are `CONTINUE`, `SUBEVENT` and `PHRASE`.

Training rows use compressed NPZ files with no pickled values:

- `input_values`: mono float waveform.
- `phone_tokens`: target phone IDs; zero is reserved for CTC blank.
- `event_labels`: frame labels.
- `boundary_labels`: frame labels.
- `timing_targets`: frame-by-two onset/offset targets; non-finite or negative
  rows are masked.
- `auxiliary_features`: frame-by-14 precomputed F0/chroma features.

Run training only on an isolated CUDA worker:

```bash
export QUALITY_V6_TRAINING_PRIVATE_KEY='base64-ed25519-private-key'
export QUALITY_V6_TRAINING_KEY_ID='quality-v6-training-2026-01'

python scripts/train_phone_event_model.py train \
  --manifest /secure/v6/manifest.json \
  --base-model-path /models/xls-r-300m-pinned \
  --base-model-sha256 "$LOCAL_MODEL_DIRECTORY_SHA256" \
  --epochs 3 \
  --learning-rate 1e-5 \
  --output-dir /secure/v6/runs/run-001
```

The command verifies every NPZ path and hash before loading the model, requires
CUDA and writes a safetensors research checkpoint plus an immutable signed
training report. That report remains `trained_uncalibrated`, `exported: false`
and incapable of runtime use. A future trusted calibration runner must create
and sign the separate calibration artifact after regression, conformal and
locked-temporal evaluation.

## Tests

```bash
cd lyricgen/backend
python -m pytest -q tests/test_quality_v6_calibration.py
```

The suite uses synthetic JSON evidence and ephemeral Ed25519 keys. It does not
import a model through Transformers, access the network or download weights.
