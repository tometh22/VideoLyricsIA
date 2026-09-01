# Machine snapshot invariant — 2026-08-31

## Confirmed staging incident

Read-only inspection of job `9f502fede03f` confirmed the blocking failure:

- status: `done`
- `jobs.segments_json`: 30 rows
- `editor_documents`: absent
- `editor_versions`: absent
- original pre-human snapshot and machine provenance: absent

The historical raw state cannot be reconstructed honestly from the approved
output.  This job must not be used as exact pre-human gold.

## Invariant for new transcriptions

The successful transcription transaction now commits all of these together:

1. `Job.segments_json`;
2. immutable `EditorDocument.original_segments`;
3. tenant-private `EditorDocument.machine_evidence`, including primary,
   independent and pre-anchor hypotheses when available, the selected machine
   output, and the machine quality/route decisions;
4. `Job.machine_snapshot_required = true`;
5. the transition to `transcribed_pending` / `editing`.

Schema v3 also freezes a song-level calibration signal alongside the machine
decision: traffic-light verdict, risk and score. While Quality v6 remains in
observe mode its raw score is intentionally null; the evidence therefore keeps
both that null and an explicitly labelled `risk_derived` score instead of
silently inventing a calibrated score.

The v3 producer freezes every completed recognition output at its recognition
route/provider boundary, before catalogue reconciliation, retry selection or formatting. Each
raw stream carries an invocation id, exact provider/model family, audio view and
transformation; rejected full-file/VAD/local-model retries remain separate, as
do concurrent intro and body runs. The selected editor output is recorded
separately. An invocation-level counter is part of the immutable evidence hash,
and any completed attempt without a named durable hypothesis blocks
approval/export.

If capture, validation or persistence fails, the transaction does not expose
the job to the editor and the transcription becomes `transcription_failed`.
Legacy synchronous paths remain in `transcribing` until the same transaction
finishes.

Approval and generate paths fail closed with `machine_snapshot_missing` for a
job enrolled in the invariant when the evidence is absent, malformed or does
not hash to `original_segments`.  Historical jobs keep their legacy behavior;
the system does not pretend that missing old raw data can be recovered.

When the exact editor version is approved, it receives a hash-bound
`training-approval-evidence-v1` payload with the approval-time traffic light and
score. Repeating approval is idempotent and cannot rewrite the first frozen
signal for that version.

## Verification

- machine evidence capture, family separation and decisions;
- snapshot hash binding and immutability;
- approval rejection when evidence is missing or mismatched;
- editor, generate, transcription-quality, evidence and correction-learning
  regression suites.
