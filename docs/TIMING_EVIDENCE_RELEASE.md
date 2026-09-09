# Timing evidence: diagnostic-only release

No retiming, text replacement, old-document migration, training or new paid calls.
The rejected local realignment experiment is NOT included in this branch.

## Changes

- Reannotation preserves `forced_align` and its timing lineage unless an explicit
  new timing source or new CTC evidence supersedes it. Authenticated content
  lineage is reused only when both its attestation and raw-output hash match.
- The Cureau adapter keeps native `probability` values and provider/model/meaning
  metadata on each word. They are explicitly not equivalent to CTC or recognition
  scores; no native-probability threshold was introduced. Repeated annotation is
  tested for complete equality and preservation of provider output.
- A gap of at least 8s between consecutive usable word stamps requires at least
  two unusable word stamps in the current/next line before being flagged. A line
  with at least two unusable words comprising half or more of its word support is
  separately flagged. Long line duration alone is never a condition. Repeated
  words are separate occurrences; absent/zero-duration words are not bridged.
- These are conservative **unvalidated-timing diagnostics**, not proof that the
  text or pause is wrong. No source word, text, start or end is removed/changed.
- The ordinary worker persists `timing_validation`; editor serialization retains
  it. The editor shows the line warning and does not say “Todo listo” or
  “Sincronizado con tu letra” for a document carrying this diagnostic.
- The quality reason is diagnostic-only and does not add paid recovery windows.
  Existing approval remains a human action per song. No new per-line clicks.

## Measured development results

Stored prehuman snapshots: Otra mujer 35 lines, Borracho 16. `diagnose` flags
6/35 and 0/16 respectively. Otra mujer index1 reproduces the 30.68s internal gap;
indices2,3,4,6,20 have insufficient usable word timing. Zero repairs are applied.

Documentary unchanged controls: 29 lines total, one alert (Otra mujer index20,
two zero-duration word stamps). These are NOT acoustic gold: that song is still
under review, so this one alert is neither a confirmed false positive nor an
independently confirmed transcription defect. Four synthetic valid controls
(long pause, repeated words, long vocalization, absent/isolated missing support)
produce zero alerts. Broader human false-positive rate is not measured.

Reproduce private snapshot audit without DB or provider access:

```sh
python scripts/audit_word_timing_snapshots.py --snapshot FIRST.json --snapshot SECOND.json --output NEW-AUDIT.json
```

Tests cover source overrides, idempotence, native probability separation,
unusable stamps, pauses/repetitions, no lyric/timing mutation, PATCH→GET metadata
persistence and the actual LyricsEditor confidence/line display. Test requests
use isolated fixtures, not real campaign songs.

## Release and rollback

Release owner: riyadh, explicit user-authorized handoff after inactive
singapore-v1 completed #1306/#1305. Release 1.1.6 preserves that staging tip. Release
VERSION/package/CHANGELOG updates and full clean-checkout CI must precede merge.
No production deployment. Runtime verification should use a disposable test
fixture or read-only code/bundle checks, never modify reviewed campaign songs.
Rollback is a revert of this release plus the previous staging frontend alias.
There is no data migration or retrospective reannotation to undo. Historical
jobs do not acquire this warning merely because this code is deployed.

Success metrics remain separate: provenance preserved; structural diagnostics
detected; false-alert rate on defined controls; **effective repairs = 0**.
