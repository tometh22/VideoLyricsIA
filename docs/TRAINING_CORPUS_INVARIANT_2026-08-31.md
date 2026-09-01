# Training corpus invariant — 2026-08-31

## One recoverable sample

A new job is a complete transcription-training sample only when it has all of:

1. immutable pre-human segments, hash-bound to machine evidence v2;
2. raw hypotheses separated by recognition family;
3. the exact approved `EditorVersion` and its approval-time quality signal;
4. every tenant-private editor checkpoint up to approval;
5. every material line delta in `audit_log`, revision-bound and untruncated.

`scripts/export_training_pairs.py` materializes that contract as private JSONL
plus a SHA-256 manifest. It is SELECT-only, never estimates missing history,
and exits non-zero with `--require-complete` if any selected sample is weak.
The JSONL contains raw lyrics and must not leave approved private storage.

## Timing labels and UI noise

`editor-line-delta-v2` records additions, deletions, text changes, relative
reorders and exact before/after line endpoints. There is no 20-line cap.

Timing movement below 50 ms is retained in the exact snapshots but labelled as
non-material in the delta. A text-only edit with a 3.7 ms renderer/editor
jitter therefore stays a text correction and no longer contaminates the timing
dataset. Pure layout metadata (`pos`, `scale`, `rot`, etc.) emits no lyric
delta.

Both editor write paths use the same builder:

- Editor V2 `PATCH /editor/{job_id}` (including fast drafts);
- legacy `POST /jobs/{job_id}/save-segments` and its durable bridge.

For machine-evidence jobs, fast drafts are stored as tenant-private checkpoints
and version pruning is disabled. Historical jobs keep the 50-version UI bound.

## Known historical loss

Job `c6553b32b6c1` proved the old gap: revision 96 retained only 52 versions and
Editor V2 wrote no `lyrics.segments_diff` rows. Missing historical snapshots
cannot be reconstructed and must not be labelled exact. The new invariant is
forward-looking from deployment.

## Canary gate before September batch

Export five newly transcribed and editor-approved jobs with:

```bash
python3 lyricgen/backend/scripts/export_training_pairs.py \
  --latest 5 \
  --require-complete \
  --output .context/training-pairs/canary-5.jsonl
```

The gate passes only when the manifest reports `rows=5`, `complete_rows=5` and
`incomplete_rows=0`. Then verify each row has at least one hypothesis family,
one approved snapshot, the two quality signals, and a revision-ordered edit
sequence (empty deltas are valid only when the operator made no material edit).
