# Bounded delivery — 2026-09-06 UTC

PR #1294, continuation of `8ec2df5e`. No live database writes, deployment,
approvals, backgrounds or videos. Experiments are development, not independent
accuracy measurements. Preview is loopback-only on the operator's Mac:
`http://127.0.0.1:8768/bounded-delivery-v1/index.html`.

## Text delivered

Six full-document views cover five songs: historical and protected-current
Bersuit, Magia Veneno, Como Caramelo de Limón, Luciano and Polizonte.
Unmodified lines remain **conserved without certification**.

| Song / line (1-based) | Before → proposed | Localization | Evidence |
|---|---|---|---|
| Bersuit / 13 | `nos vienen` → `no vienen` | 99.08–104.72 s | Previously stored independent blind Whisper/Gemini; bounded CTC, no display-time change |
| Bersuit / 29 | `estripartito` → `es tripartito` | 209.92–214.76 s | Stored two-family blind audio; development case, not independent validation |
| Magia Veneno / 37 | `semborrachar` → `se emborrachar` | 144.38–149.02 s | Both new blind families produce this tokenization; grammatically questionable, **human decision still needed**, not a verified final correction |
| Como Caramelo de Limón | No supported change | Four sampled windows | Retained abstentions; not certified correct |
| Current Bersuit / Luciano / Polizonte | No changes | Cached controls | Protected-current Bersuit's 38 locked lines remain untouched |

The two additional songs are revision 0 with no protected lines, from the
development partition. No reserved test song was used. Only four windows per
additional song were acoustically inspected; complete candidate documents do
not imply full-song acoustic coverage. Realignment is localization of supplied
text, not an independent audio witness. The production adapter now uses the
same occurrence-bounded realignment, preventing the wider listening context
from attracting alignment into the following phrase.

Observed new work: 16 paid-provider requests, all valid responses; 62 reported
Whisper seconds, 2,734 Gemini input tokens and 1,770 output tokens. Per-song
wall times: 37.413 s and 26.801 s, including one 7.157 s local CTC realignment.
Invoiced USD unavailable. Existing Bersuit/control evidence was reused.

## Endpoint decision: exactly two approaches

Same decoded mix SHA and frame lattice verified. Enlarging singing-detector
context by two seconds preserves all tested interior probabilities exactly
(median absolute difference 0) but changes boundary probabilities by up to
0.982. Its 1.643 s receptive field blurs transitions; this is **not** a measured
perceptual latency. Stem/mix transfer remains blocked and was not used.

A: three strongest later local CTC exit-path peaks. B: largest learned-activity
fall interval, then the strongest CTC exit inside it. Both retain baseline,
never shorten, and are bounded by the following occurrence. Activity does not
prove continuation of the same word. No automatic-selector threshold changed.
Predictions were saved before the script loaded the historical comparators.

| Bersuit line | Baseline | Historical comparator | A alternatives | B |
|---|---:|---:|---|---:|
| 7 | 55.150 | 55.4379 | 55.200 / 55.160 / 55.320 | 55.200 |
| 34 | 238.490 | 239.7476 | 239.580 / 239.720 / 239.260 | 239.580 |
| 41 | 283.520 | 284.1059 | 283.540 / 287.720 / 287.600 | 283.540 |

A contains a candidate within 150 ms in 2/3 cases, **not** a selector achieving
that result. Its top choice reaches that tolerance in 0/3. Two alternatives
on line 41 worsen the comparator error by several seconds. B is closer in
3/3, but reaches 150 ms in 0/3 and adds little beyond the CTC ranking.
All three comparators are auto-trim-contaminated development evidence, not gold.

Luciano no-damage control: baseline 179.320; A proposes 181.960 / 181.060 /
182.100 despite no established defect. B proposes 179.540, also unverified.
Baseline stays unchanged. **Reject A as an operational selector; B has no
demonstrated independent selection benefit.** Keep the alternatives as offline
diagnostics, not enabled suggestions or automatic repairs. No third approach
or spectral retuning was attempted. The remaining technical blocker is
identifying the perceptual ending of this word, not generating later numbers.

## Operational verification and activation

- Real Chromium: six choices, full audio, two Bersuit changes, playback beyond
  the old line end, zero page errors. This is readonly preview QA, not staging.
- Local SQLite integration: human candidate adoption creates a version without
  approval or machine-added locks; the existing subsequent song approval works.
- Normal-save capture: baseline/submitted timing, audio identity and author
  recorded once; machine candidate adoption excluded. Not blind or clean gold.
- Frontend component tests exercise one-click whole-candidate adoption and
  exposure/examination telemetry; ignoring is not rejection.
- `REVIEWER_ASSIST_ENABLED` and `REVIEWER_TIMING_CAPTURE_ENABLED` remain default
  off. No authorization for this PR's staging deployment was found. Activation
  requires an explicit staging release approval and verified post-auto-trim
  capture epoch; production remains out of scope.

Artifacts (private workspace): `bounded-delivery-v1/` holds full candidates,
requests, usage, clips and browser evidence; `endpoint-decision-v1/` holds
expanded activity frames, immutable predictions, comparator evaluation and
Luciano control. Source snapshots and original evidence were not overwritten.
