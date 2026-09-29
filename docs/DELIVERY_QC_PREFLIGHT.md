# Delivery QC preflight

## Goal

Catch label-facing defects before a lyric video leaves Genly.  The report uses
the useful semantics visible in the UMG example: OPEN/RESOLVED lifecycle,
PASS/WARN/FAIL severity, issue category, frequency and frame timecodes.

This is a second quality boundary.  Transcription Quality v6 audits the
transcription and timing evidence; Delivery QC audits what is about to be
delivered.

## Checks available now

- metadata title/version and artist versus the render manifest/title card;
- final displayed spelling and diacritics versus a trusted approved lyric;
- a reference-free deterministic Spanish pass for fixed accents, uppercase
  accents, future endings (`-ás`/`-á`/`-án`) and audited near-exact dictionary
  typos such as `AVENTRUA`/`AVENTURA`;
- invalid ranges, overlaps and lyrics outside the media duration;
- reference-free review warnings for runs of fragmented one/two-word cards,
  inconsistent near-repetitions and cards that end before their final stored
  word timestamp;
- blocking of sentence periods at the end of delivered lyric lines;
- propagation of the upstream Quality v6 verdict;
- blocking of unattested catalogue text and materially incomplete timelines
  from `reference_health`;
- aggregation of repeated occurrences with non-drop frame timecodes.

A catalogue result is not accepted as truth.  If the lyric was not supplied by
the client, verified by an operator or supported by the evidence policy, the
song-reference comparator abstains.  The narrower deterministic Spanish pass
still runs without a reference and emits human suggestions only.  It excludes
ambiguous homographs (`si`/`sí`, `tu`/`tú`, `aun`/`aún`) and never changes a
line automatically.  This is especially important for live songs.

## Product integration implemented

- The exact final MP4 is inspected before R2 upload on initial and edited renders.
- `ffprobe` validates streams, duration, codec, pixel format, dimensions and FPS.
- For UMG deliveries, Gemini OCR samples the title card and lyric frames by
  default (`DELIVERY_QC_UMG_OCR_ENABLED=0` disables it). Other deliveries retain
  the opt-in `DELIVERY_QC_OCR_ENABLED` flag. The model never receives expected
  text; comparison happens locally and OCR never changes the video. Empty or
  low-confidence OCR is `NOT_RUN`, never `PASS`.
- Reports persist on `Job.delivery_qc`, are bound to segment revision/hash and
  become `STALE` on every editor mutation.
- Every check exposes an explicit result: `PASS`, `FAIL`, `REVIEW` or
  `NOT_RUN`. `PASS` means the detector ran and found no finding; `NOT_RUN`
  makes disabled or unavailable automation visible instead of treating it as
  a clean video.
- Job Detail shows a clickable checklist, frame seeks, acknowledgements, safe
  repair buttons and a direct editor link.
- Reviewer decisions, accepted repair types, approval outcome and later label QC
  finding counts are structured product events. Existing editor `active_edit_ms`
  supplies minutes-per-song before/after.
- `DELIVERY_QC_MODE=observe` never blocks ordinary approval. UMG publication
  always requires a fresh report, correction of objective open `FAIL`
  findings, and one audited human attestation for the complete current render.
  The attestation resolves the grouped visual/listening checklist in one
  operation; generic reminders are not displayed or signed as individual
  issues. Other open `REVIEW` findings remain recommendations and never become
  automatic failures.
- A delivery version is `NOT_APPLICABLE` when the selected UMG specification
  does not require one; when it does, missing visible evidence is `NOT_RUN`.

## Deliberately not automatic yet

- black/frozen-frame, loudness and clipped-audio thresholds need the exact label
  delivery specification and observe-mode calibration; black/still imagery can
  be intentional;
- OCR mismatches and small timeline overlaps are proposal-only;
- text suggestions derived from a reference are exposed only when that reference
  is independently attested and bound to the current segment hash;
- automatic correction remains behind per-category acceptance evidence.

The Spanish pass is `observe` by construction: its candidates carry
`automatic_apply_allowed=false` and are persisted in the existing one-click
operator proposal.  A category may gain automatic authority only after at
least 50 reviewed songs and measured precision of at least 99%; authorization
is per category, never global.

## CLI

```bash
python lyricgen/backend/scripts/run_delivery_preflight.py manifest.json -o report.json
```

The manifest contains `metadata`, `asset`, final `segments`, optional
`approved_lyrics`, `reference_trusted`, upstream `quality`, and `fps`.

## Transactional Repair Agent

`delivery_repair_agent.py` consumes the same manifest and applies one bounded
repair at a time to a copy.  Every candidate is immediately re-audited.  It is
committed only when the fresh preflight improves and introduces no higher-risk
finding.

```bash
python lyricgen/backend/scripts/run_delivery_repair.py \
  manifest.json -o repair-result.json
```

Current automatic repairs:

- title, version and artist in the render plan from authoritative delivery
  metadata;
- accents/diacritics against a trusted approved lyric;
- near-exact token typos against a strongly matched trusted line;
- event ends that exceed the known media duration.

Small overlaps are intentionally **proposal-only**. A retrospective UMG run
found one improved case and one damaged case; a tiny aggregate timing gain is
not sufficient to grant mutation authority.

The output preserves the original manifest, an action per occurrence, before
and after reports, confidence, patch, reason, and requirements before delivery.
Text or timing mutations always require a new Quality v6 analysis and a fresh
preview.

Ambiguous vocabulary, missing phrases, long overlaps, zero-duration events and
acoustic endpoints remain proposals unless an independent verifier reaches the
configured threshold.  Verifiers are injected callbacks so CTC/stem+mix or a
future provider can be benchmarked without granting it unrestricted mutation
authority.

Reference integrity failures are `ESCALATED`, not presented as editable
corrections: the appropriate action is to find another witness, recover the
missing performance or ask an operator.  Timing changes on live recordings are
also proposal-only unless an independent verifier confirms them.

## Product loop

1. Motor produces segments and independently attested suggestions.
2. Render produces the exact customer-facing asset.
3. Preflight audits the encoded asset and persists an actionable report.
4. The reviewer resolves objective failures, accepts safe suggestions, and
   confirms the complete visual/audio review once for the current render.
5. A correction creates a normal edit render; the old report becomes stale and
   a new preflight is mandatory.
6. Approval records internal QC counts. Later label feedback is recorded through
   `/jobs/{job_id}/delivery-qc/external-result`, making zero findings measurable.

### Temporary staging review-gate exception

For an explicitly named staging campaign only, operators can temporarily skip
the generic signed-review checklist when a **fresh** QC report exists. Objective
blocking `FAIL`s, stale/missing reports, the independent language-discrepancy
gate, required render files and ProRes freshness remain enforced. This is not a
QC pass and does not alter production behavior. Each approval and portal publish
records `staging_manual_review_bypass=true` in its audit event.

The exception is disabled unless all three staging environment variables are
set: `DELIVERY_QC_UMG_STAGING_REVIEW_BYPASS=1`,
`DELIVERY_QC_UMG_STAGING_REVIEW_BYPASS_CAMPAIGN_IDS` containing the exact
campaign IDs, and `DELIVERY_QC_UMG_STAGING_REVIEW_BYPASS_UNTIL_UTC` containing a
timezone-aware UTC expiry. It fails closed outside `ENVIRONMENT=staging`, for
unlisted campaigns, or after expiry. Remove the flag, IDs and expiry once the
local review flow is ready; never add these variables to production.

The first rollout must measure precision per repair type and minutes of human
correction saved.  Enforcement is enabled per repair type, never as one global
switch.

## Connection to the transcription/timing engine

This is now part of the final transcription path, behind two default-OFF flags:

```bash
REFERENCE_ATTESTATION_MODE=observe
DELIVERY_REPAIR_SHADOW_MODE=observe
```

The first flag compares catalogue text with clean, audio-first WhisperX before
catalogue vocabulary or structure can own reconciliation/forced alignment. In
`enforce`, an unsupported studio reference falls back to raw audio-first ASR.
For live recordings, catalogue text can only assist local vocabulary; the
recorded performance always owns order and timing.

The second flag runs the Repair Agent after all transcription post-passes and
Quality v6. Its candidate diff is stored inside the persisted quality envelope
for the editor, while the actual job segments remain byte-for-byte unchanged.
An unattested reference makes the agent abstain.

### Measured shadow result (UMG gold v1)

- 65 human-approved deliveries were located; 57 also retain the pre-human
  machine snapshot and are directly scorable.
- WER moved from 8.298% to 8.093% (2.47% relative improvement).
- CER moved from 6.858% to 6.799% (0.86% relative improvement).
- Six songs improved, zero songs had worse text, and timing stayed unchanged.
- Decision: `GO_SHADOW`, not automatic rollout.

The result is retrospective because it uses the approved human lyric as the
trusted reference. Production-like validation must first attest a catalogue
candidate against independent ASR, then measure accepted/rejected suggestions
and editor minutes saved.

### Timing policy

Pitch-tail correction requires agreement between stem and mix and remains
default-OFF. It correctly abstained on the supplied Bersuit and La Renga live
recordings: their dominant failures are missing/wrong performance structure,
crowd vocals and unsafe reference ownership, not a simple sustained last-word
tail. Fixed overlap clamping is not enabled.
