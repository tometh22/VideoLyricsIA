# Audio-first transcription rollout

## Decision

The first Whisper pass listens to the audio with a generic language prompt.
Curated lyrics are applied only after ASR, when word timestamps are aligned to
human line structure.

This reverses the legacy order:

```text
legacy: reference → Whisper prompt → segment timestamps → synthetic recovery
new:    audio → Whisper word timestamps → validated reference → line alignment
```

The change is motivated by the 94-song production corpus and the regression
track `La Foto de tu cuerpo_ Rodrig.wav`. On that track, reference prompts
collapsed Whisper while the generic pass nearly matched ROTOR.

## Safety properties

- Word timestamps are requested on every Whisper-1 orchestrator path.
- Raw segment display bounds use the first and last timestamped word.
- A positive silence gap never extends the previous lyric.
- VAD retries never reuse a suspect reference prompt.
- Synthetic recovery requires at least 20% anchored lines (minimum four),
  broad anchor coverage, and no output segment longer than 15 seconds.
- A title-only LRCLIB candidate is accepted only after ASR and only when title,
  duration, and heard words agree.
- The primary LRCLIB lookup requires the full song title after normalizing
  known edition suffixes; a short title cannot select a longer, different song.
  The same check applies to cached entries.
- External lyrics must pass independent ASR attestation before they can replace
  words or determine the whole-song structure. An unsafe candidate falls back
  to the audio transcript even if the diagnostic setting is `observe` or `off`.
- A live `local_only` verdict emits the audio transcript before any global
  catalogue reconciliation, regardless of the other live policy switches.
- A later Genius/Gemini gap refetch must pass the same audio attestation before
  it can replace an already accepted lyric.
- Catalogue text cannot prime first-pass ASR or choose its language before the
  audio has been heard.

## Operational switches

```dotenv
# Default: reference cannot prime first-pass ASR.
WHISPER_REFERENCE_PROMPT_MODE=off
# The user-facing orchestrator ignores non-off prompt modes so its ASR
# witness remains independent of metadata-based lyrics.

# Default: align curated lines to Whisper word timestamps.
LRCLIB_PLAIN_ALIGNER_ENABLED=1
# Emergency kill-switch: 0.
```

`LYRIC_LEAD_IN_S=0.2` is a presentation experiment, not part of the correctness
cutover. It may be canaried separately after timing accuracy is stable.

## Acceptance gates

Per job:

- no empty transcription;
- no unreviewed synthetic recovery below the anchor gate;
- maximum ordinary lyric segment under 15 seconds;
- aligned-reference coverage at least 50% and at least eight confident lines;
- timing source always populated.

Regression track:

- no reference-prompt collapse;
- no synthetic recovery;
- all 28 ROTOR lines find a monotonic textual correspondence;
- median absolute start/end error against ROTOR below 0.5 seconds;
- maximum segment below 7 seconds.

Corpus:

- five paired runs per arm on the 94-song corpus;
- arms: legacy reference prompt vs audio-first;
- primary outcome: per-song collapse probability;
- secondary outcomes: approved-text WER/Jaccard, aligned-line coverage,
  p95 maximum segment duration, synthetic-recovery rate, and operator edits;
- cluster bootstrap by song and a mixed-effects logistic model for collapse;
- freeze every raw ASR response so downstream pipeline comparisons are
  deterministic and separately report online ASR variance.

## Rollout

1. Shadow audio-first on 10% of eligible jobs while legacy remains user-facing.
2. Require at least 100 jobs and no safety-gate regression.
3. Canary user-facing output at 10%, then 50%, then 100%.
4. Hold each stage for at least one full operator review cycle.
5. Roll back via a coordinated release if the audio-first path regresses; the
   catalogue prompt cannot be re-enabled for user-facing recognition.
   The plain-line aligner can still be disabled with
   `LRCLIB_PLAIN_ALIGNER_ENABLED=0`; keep the instrumental-gap fix.

## Observed regression result

Before:

- six raw segments with the correct reference prompt;
- 31 synthetic lines from two anchors;
- maximum segment approximately 115.87 seconds.

After:

- 30 audio-aligned lines with corrected metadata;
- no synthetic recovery;
- maximum segment 5.20 seconds;
- 28/28 ROTOR lines matched;
- median absolute start/end error 0.255/0.335 seconds.

With the original truncated artist (`Rodrig`), audio-evidence lookup recovered
the `Rodrigo Romero` LRCLIB record at score 0.956 and produced 31 lines (one
explicitly marked for review), again with a 5.20-second maximum.
