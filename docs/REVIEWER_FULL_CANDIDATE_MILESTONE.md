# Complete isolated candidate milestone — 2026-09-05 ART

## Delivered artifacts and actual outcome

The first repaired complete candidate uses the immutable **prehuman Bersuit
revision 0**, not the current reviewed document. Three additional complete
candidates use the same frozen current snapshots as the canary.

| Candidate | Lines | Text changes | Display timing changes | Protected rows preserved |
|---|---:|---:|---:|---:|
| Bersuit, prehuman development copy | 41 | 1 | 0 | Not a current human document |
| Polizonte, current snapshot | 28 | 0 | 0 | 0 |
| Hoy le pido a Dios, current snapshot | 53 | 0 | 0 | 0 |
| Bersuit, current snapshot | 41 | 0 | 0 | 38/38 |

Zero changes in a current candidate does **not** certify that song. Every
unmodified row is explicitly preserved without certification. No current
document, approval, queue entry, background or video was modified/generated.

Private outputs: `reviewer-shadow-artifacts/full-candidates-v1/manifest.json`
and one JSON per candidate, each containing baseline + candidate, their full
SHA-256 hashes, changes, evidence, whole-song structural checks, audio hypothesis
comparisons, unresolved decisions and orthography findings. V1 is exploratory
worktree output; use the subsequent committed V2 replay for pinned execution.
`index.html` provides read-only comparison against **full original audio**.
No additional human annotation session is a prerequisite.

## Why the text repair worked

Two reference-free recognizers independently heard the replacement for a fused
word, with matching neighboring words. The minimal-patch selector changes only
that span and preserves unrelated spelling/case/punctuation. It requires two
families and a unique two-word adjacent anchor; forced alignment cannot vote.
The later human correction is compared only after inference. It matches this
development example but is not independent precision evidence or clean gold.

The second historical discrepancy is retained: the Whisper clip did not provide
the target phrase, while Gemini did. A single family cannot authorize the patch.
The successful patch was realigned by the existing pinned Spanish CTC on the
original mix. Word clocks remain evidence; no new display endpoint was adopted.

## Directed endpoint experiment

Luciano, zero-based line 50:

| Search | CTC candidate end | Result |
|---|---:|---|
| Original 175.50–181.32 s | 178.82 s | Initial hypothesis |
| Widened 175.50–189.32 s | 188.10 s | Repeated-occurrence drift |
| Widened only to next-occurrence hypothesis at 184.30 s | 178.82 s | Prefix anchors stable, maximum shift 20 ms |

After excluding the following occurrence, pYIN measured a periodicity/voicing
transition interval of **179.07–179.08 s** near the aligned last word. Current
display end is 179.32 s. This does not prove a clipped ending in this case, nor
does it certify voice identity or the perceptually acceptable endpoint. No
timing change was applied. Detector configuration and every frame are retained
in `sustain-occurrence-v1.json`. The next-onset ceiling is itself a baseline
hypothesis, not a new acoustic onset certificate.

This isolates occurrence confusion from sustain detection. There is no evidence
yet that a objectively correct timing candidate was rejected by the selector.
No threshold relaxation, universal padding or timestamp transfer from MP3 stems
was introduced.

## Normal-workflow implementation

- Existing quality worker calls the reviewer only behind
  `REVIEWER_ASSIST_ENABLED=1`, and only when no native proposal batch was built.
- Existing text rollout gate remains required. Persistent
  `REVIEWER_ASSIST_CACHE_DIR` must be configured; missing cache causes zero paid
  calls and an explicit reason. Audio hashes must match before listening.
- At most four eligible, already-flagged windows per run. Locked lines and
  approved songs are skipped. Results/attempt reservations are cached by source,
  clip, model and configuration. Text repairs are realigned; endpoint clocks are
  not silently adopted.
- Existing operator-proposal persistence enforces revision/audio checks and
  preserves an existing proposal. The full candidate is stored separately from
  the current document until an explicit human action.
- Existing panel can show the full candidate and use all candidate changes with
  one action, followed by the existing single song approval. Per-change accept
  buttons are not mandatory. An uncalibrated label replaces confidence claims.
- A human revision makes the complete pending candidate stale instead of
  silently rebasing its full text. Subsequent edits after acceptance emit
  operational evidence through the existing ProductEvent path.
- Candidate adoption is versioned with reason `reviewer_candidate`, not as a
  human-certified correction; machine changes are not marked locked or approved.
  Generated receipts are server-side events with no human author attribution.
- Per-window viewport exposure, first interaction and bounded active checking
  time use the existing analytics endpoint; a loaded batch is not automatically
  counted as viewed. Existing acceptance/manual-edit/rejection events are reused.
  Ignored proposals are not rejections. Analytics delivery remains best-effort;
  completeness and causal time savings have not been demonstrated.

**Not deployed or enabled.** The real provider/audio experiments ran offline;
worker wiring was tested with controlled audio responses, not enabled in staging.
The preview's browser check is not a human content/timing judgement.

## Whole-song QC scope and unresolved limits

All candidate rows, including unmodified rows, were checked for finite in-range
times, overlaps, empty lexical content and protection changes. Full available
audio hypotheses and the associated external reference were compared across the
whole song. This does not independently verify the performance: omitted singing,
wrong repeated occurrences and lead/chorus boundaries remain incompletely tested.
The chronological occurrence ledger keeps repeated phrases distinct and stores
alternative hypotheses; it is not falsely labeled an acoustic PerformanceGraph.
Full acoustic coverage and trustworthy sung endpoints remain open.

Reference reconciliation is **145 distinct jobs with present text and a unique
metadata association**, 9 present rows unmatched, 131 matched missing markers,
9 unmatched missing markers. Both original and reconciled import JSONs agree.
154 is the total present-text count; 144 is not supported by those manifests.
Metadata association does not certify the exact recording or the external text.

## Evidence and measurement limits

Python 3.11.15, pinned existing CTC checkpoint, no new weights downloaded.
Candidate builds reuse stored provider results: **zero new paid calls** for
building the complete versions. The preceding two development clips used four
real calls whose responses, usage and latency remain in `text-edit-replay-v1`.
Invoice-backed dollar cost is not confirmed. Local build/alignment latencies are
recorded individually; no production throughput inference is made from them.

Final targeted checks: **133 backend tests passed**, two PostgreSQL-only
checks skipped locally; **ten panel tests passed**. JUnit evidence is retained
in `reviewer-shadow-artifacts/full-candidate-tests.xml`. Browser smoke: full 289.413333-second
Bersuit audio, seeking to the repaired phrase, switching from 41 to 53 rows,
zero JavaScript errors. No human accuracy or time-saving result exists yet.

The **2 minutes/song** target remains a product hypothesis, not an achieved result.
