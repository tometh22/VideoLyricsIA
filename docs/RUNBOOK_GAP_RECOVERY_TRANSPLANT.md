# Runbook — Gap-recovery hardening + acoustic-twin transplant (R&D)

**Status:** R&D. Branch `feat/gap-recovery-transplant-rnd` (commit `0fb5dca`).
All behavior is behind flags, **default OFF**. NOT in staging/prod. No prod
behavior change until flags are enabled.

Lab validation: "Nada Fue Un Error (En Vivo)" (Coti) + 3 regression songs
(Mujer Amante, Una Vez Más, De A Ratitos). Tests: 30 (test_gap_recovery.py,
test_llm_segment.py).

---

## What's in the commit

| Piece | Flag | Default | Effect |
|---|---|---|---|
| Event-loop offload | (always on) | — | gap-recovery/LLM-segment run in `asyncio.to_thread` so they don't freeze the API event loop. No-op when the gated callees self-decline. |
| Divergence gate | `LLM_SEGMENT_DIVERGENCE_RATIO` | 1.25 | On reconcile-abort, LLM-segment only preempts forced_align when the recording sings ≥1.25× the canonical word count (true live). Protects studio songs with mishears (incident "Viejas Locas — 638"). |
| Path 1: forced-align gap timing | `GAP_RECOVERY_ALIGN_ENABLED` | off | Recovered gap text is forced-aligned to the clip → real per-word timing instead of uniform split. Lab: 2.01s → 0.73s timing error vs Rotor. |
| Stage 3: acoustic-twin transplant | `GAP_RECOVERY_TRANSPLANT_ENABLED` | off | Fills a crowd-sung chorus gap by copying the lead's earlier clean chorus (chroma subseq-DTW + linear warp). Gated by: vocal-presence (RMS), smear guard, vocab containment ≥0.5, beats ≥8. Falls back to the Gemini path on decline. |

Tuning knobs: `GAP_TRANSPLANT_MAX_COST` (0.12), `GAP_TRANSPLANT_MAX_WORD_S` (1.2),
`GAP_RECOVERY_MAX_GAPS` (4), `GAP_RECOVERY_MIN_GAP` (8.0).

---

## Staging rollout (suggested order)

1. Enable `GAP_RECOVERY_ALIGN_ENABLED=1` alone → validate gap timing.
2. Add `GAP_RECOVERY_TRANSPLANT_ENABLED=1` → validate transplants on live tracks.
3. Watch for `[GAP-TRANSPLANT]` / `[GAP-RECOVER]` logs (transplanted vs forced-aligned vs uniform counts).
4. Anything looks wrong at any step → flag to 0 (instant).

## Rollback (4 levels)

1. **Branch isolation** — not merged: `git branch -D feat/gap-recovery-transplant-rnd`.
2. **Flags OFF** — merged but flags off → no prod change.
3. **Runtime kill switch (no redeploy)** — `GAP_RECOVERY_TRANSPLANT_ENABLED=0`,
   `GAP_RECOVERY_ALIGN_ENABLED=0`, `LLM_SEGMENT_ENABLED=0` (env-driven on Railway).
4. **`git revert 0fb5dca`** — if already merged/deployed.

---

## Findings (the R&D learnings — don't re-derive these)

- **Timing is already Rotor-level where the word is right.** On confidently-
  matched lines, median |Δ start| vs Rotor = **+0.05s** (σ 0.16). The gap vs
  Rotor is TEXT, not timing.
- **The misses are crowd choruses + ASR limits, not our timing.** whisperX drops
  the loud crowd-sung choruses; the demucs *vocal stem strips the crowd*, so we
  run blind there. The mix has 2.5–4.7× more energy in those gaps than the stem.
- **Held notes** (e.g. 168–184s in Nada) are sustained vocals Gemini labels as
  "(grito)"; Rotor *stretches* a line over them (a display choice, not ASR).
- **Live word changes** ("error"→"mejor", "dolores"→"valores"): both whisperX
  AND Gemini hear the studio word independently — likely Rotor is the outlier or
  uses human QA. Not fixable with this ASR stack.

## Known limitation — verse↔chorus wrong-twin (UNSOLVED, residual risk)

The transplant can match a verse gap to a chorus (they share chords) and copy
the wrong lyrics. **Three discriminators were calibrated and ALL FAILED on this
crowd-heavy vocal stem:**

| Discriminator | Result on real song |
|---|---|
| F0 melody-contour DTW | verse-chorus 0.98 < chorus-chorus 1.18 (backwards) |
| Chroma cost threshold | verse 0.081 sits BETWEEN choruses 0.075/0.097 |
| Force-align text→gap-audio score | wrong text 0.567 > correct text 0.533 |

Root cause: acoustic similarity ≠ lyric identity, and the crowd-stem F0 is too
noisy. Mitigated in practice: gaps are usually choruses (not verses), flag is
OFF, and decline falls back to Gemini. **Do not add another acoustic gate — it
won't discriminate.**

## Roadmap (the real fixes — heavier, not done)

1. **lrclib content prior** — derive which lines are the chorus (repeat in the
   synced lyric) and snap transplant text to canonical; doesn't need clean audio.
2. **SSM structure on the FULL MIX** (not the vocal stem) — segment verse/chorus
   and only transplant within the same section cluster.
3. **Keep all 4 demucs stems** — the crowd lives in `other`; use it as a
   redundant timing/presence source for crowd choruses.
4. **Cross-recording studio alignment** — align live↔studio (cover-song DTW) to
   transfer perfect lyrics; needs a real studio file (the Downloads "studio" was
   the live itself).
5. **Confidence surfacing** — where whisperX/Gemini/transplant disagree, flag for
   operator review (the cheap path to Rotor-grade QA).

Process note: this was validated via 2 adversarial review rounds (5 agents each)
+ a 5-way debate; the discriminator failures above are the debate's empirical
conclusion.
