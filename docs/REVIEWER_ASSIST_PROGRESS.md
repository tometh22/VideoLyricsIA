# Supervised reviewer: implementation status

Scope: lyrics and timing only. No media generation, auto-application, training,
approval changes or production publication occurred in this experiment.

## Implemented

- Production pinned Spanish CTC primitives integrated for bounded single-phrase
  localization on decoded original mix. Explicit local/global word clocks,
  model revision, latency and tool errors. Alignment does not certify text.
- Separate bounded-context policy: evidence required, at most eight extra
  seconds and 24 seconds total. No additional provider calls in this replay.
- Full audio hypotheses reused for lexical occurrence enumeration. Repeated
  occurrences are not silently assigned to the first matching phrase.
- Supervised bridge `reviewer_assist.prepare/publish` to the existing operator
  proposal schema. `REVIEWER_ASSIST_ENABLED=0` by default. Existing text/timing
  rollout gates still apply. Existing proposals are preserved, not overwritten.
- Source revision/audio invalidation, selector revalidation, locked protection.
- Operational receipt reducer distinguishes generated, shown, examined,
  accepted, edited, rejected and unexamined; does not estimate precision/savings.
- Preview malformed option tags fixed, with regression and browser check.

## Executed on real audio

Private artifacts under `.context/reviewer-shadow-artifacts`:

- `localization-v1.json`: same three songs, eleven windows, eleven CTC candidate
  endpoints. Four repeated-occurrence blockers; four phonetic-evidence blockers;
  three protected human lines. Zero selected/published changes. Unique lexical
  occurrence plus CTC is only provisional localization, not independent proof.
- `clock-audit-v1.json`: all three mixes are PCM WAV; all three stems contain MP3
  despite a `.wav` suffix. Metadata and decoded sample counts recorded. Provider
  encoder delay/padding has not been proven; no stem-to-mix transfer authorized.
- `prehuman-replay-v1.json`: three frozen Bersuit controls aligned from revision
  zero before reading later human endpoints. Comparisons are development-only;
  historical auto-trim contamination excludes them from clean gold.

Initial runs used Python 3.11.15 and the modified draft worktree based on
42c2ca5e6ff8080339df0d64c1a7a12e58a038e9; that base SHA alone does not identify
the new uncommitted implementation. Subsequent committed replays should record
their exact implementation commit. No new model weights or paid calls required.

## Not yet complete — do not present as enabled product

- The supervised bridge is callable but not connected to the normal job worker.
- Occurrence anchors for repeated phrases and reliable sung-end evidence remain
  unresolved. CTC endpoints are candidates, not publishable fixes by themselves.
- Receipt aggregation is implemented; actual viewport exposure, per-proposal
  active time and subsequent-edit attribution still need end-to-end wiring.
- Existing panel supports playback/comparison/apply/reject; the agent-specific
  evidence and edit workflow have not yet been browser-tested in that panel.
- No suggestions from this agent were shown to Agus. No human accuracy result
  or causal time-saving claim. Separate annotation is not a prerequisite for
  further implementation or the eventual supervised trial.
- Reference intersection is 145 present + unique metadata candidate, not 154.
  These are metadata matches, not 145 audio-verified recording associations.
  Nine present rows and nine missing-marker rows remain unmatched. The asserted
  144 needs row-level reconciliation before it can be adopted as a denominator.

## Local verification

Run with the dedicated Python 3.11 environment, from this worktree:

```sh
PYTHONPATH=lyricgen/backend /Users/tomi/conductor/workspaces/VideoLyricsIA-main/riyadh/.context/venvs/shadow-py311/bin/python -m pytest lyricgen/backend/tests/test_reviewer_assist.py lyricgen/backend/tests/test_reviewer_shadow.py lyricgen/backend/tests/test_shadow_reference_import.py -q
```

Result: 34 tests passed. This is targeted verification, not full release CI.
