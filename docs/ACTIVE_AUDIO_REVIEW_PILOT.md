# Active listening pilot

This local pilot adds an adaptive second pass to Genly's existing blind audio
adapters. It uses Gemini and Whisper; it does not add a second transcription
stack or change the live pipeline. No database writes, approvals, renders or
publication occur. Reports are consumable by an external agent, but this is
not yet an authenticated API or browser integration.

## Model choice

Use the existing `BlindAudioTools` provider implementations initially. The
published [Music Flamingo model card](https://huggingface.co/nvidia/music-flamingo-2601-hf)
states non-commercial research only (checked 2026-11-17). It is not selected
for the Genly operational pilot. Its superiority for our Spanish singing
catalogue has not been established either.

## What the pilot does

1. Verify the original audio's SHA-256 against a frozen transcript snapshot.
2. Listen in windows of at most 24 seconds without exposing title or lyrics to
   the providers. Preserve source offsets and provider response provenance.
3. Locate discrepancies with the frozen transcript and revisit up to six
   affected lines, using a new crop with two seconds of surrounding context.
4. Suggest text only when both provider families agree across baseline and
   focused crops. Repeated requests to one family, cached identical clips,
   unstable readings and boundary-crossing phrases are insufficient.
5. Return a private JSON report with observations, coverage gaps, proposals,
   pending acoustic verification and usage metadata. Never claim model timings
   are calibrated acoustic boundaries. Text proposals require realignment.

`TEXT_SUPPORTED` only means normalized text agreement. It does not certify
punctuation, timing, completeness of the song or the correctness of both models.
`TEXT_PROPOSAL` is a review candidate, not permission to apply it. An empty
finding list is not approval. Omissions outside existing lines are retained in
`issues` but not automatically turned into new segments.

Long phrases exceeding 20 seconds are not silently truncated for focused
listening. They remain pending. The focused pass is limited to the first
discrepant lines within its configured budget. Coverage is measured from valid
responses, not attempted requests. `COMPLETE` refers to coverage for requested
providers, not successful resolution of findings or independent evaluation.

## Run

Requires Python, ffmpeg/ffprobe, the backend's existing dependencies, and the
existing provider credentials in the process environment. Google uses
`GOOGLE_APPLICATION_CREDENTIALS` (Vertex service-account file), optionally
`VERTEX_PROJECT` and `VERTEX_LOCATION`. Whisper uses `OPENAI_API_KEY`.
Credentials stay outside snapshots and reports.

Snapshot shape:

```json
{
  "job_id": "actual-job-id",
  "tenant_id": "actual-tenant",
  "revision": 3,
  "audio_revision": 1,
  "audio_sha256": "SHA-256 of the exact original file",
  "segments": [{"start": 4.0, "end": 6.0, "text": "Original line"}]
}
```

Use revisions from the actual source when preparing an operational snapshot.
Do not use a historical experiment report to patch a live song. Before any
future application, revalidate audio and segment revisions and the segment hash.

From the repository root, preflight (no provider calls):

```sh
python3 lyricgen/backend/scripts/run_active_audio_review.py \
  --snapshot .context/pilot/snapshot.json \
  --audio .context/pilot/original.wav \
  --output .context/pilot/run-01
```

Add `--execute` to run, with optional `--max-calls 48 --max-focus-lines 6`.
`--providers openai` is a useful connectivity smoke test, but cannot produce
two-family text proposals. A bounded run may leave much of the song unheard;
inspect `coverage` and `focus_deferred` before drawing conclusions.

The request budget bounds attempts including failures and cache hits, while
`calls` counts newly purchased requests. A third of the attempt budget (capped
by the focused-line budget) is reserved for revisits. Existing adapter caching
also prevents automatic repurchase after an interrupted call. Final reports
are exclusive files; choose a new output directory for a separate experiment.
Costs are unknown unless measured, never inferred to be zero.
Authentication/permission/quota failures (401/403/429) stop further requests to
that provider for the run. Other providers can continue within the same budget.

The first three-song staging-authenticated diagnostic has now run. See
[results and limitations](ACTIVE_AUDIO_REVIEW_RESULTS_2026-11-17.md). Staging
credentials resolved the local authentication blocker described below, but
Vertex capacity failures left the dual-provider experiment incomplete. The
pilot produced no corrections; do not interpret zero regressions as useful
automation or enable automatic application on this evidence.

Tests without product/database fixtures:

```sh
PYTHONPATH=lyricgen/backend python3 -m unittest discover \
  -s lyricgen/backend/tests -p test_active_audio_review.py -v
```

## What remains to establish

Functional tests do not establish recognition quality. Compare the baseline
and adaptive pass on held-out songs/artists using exact historical correction
lineage. Count useful changes, harmful changes, abstentions and provider cost.
Unreclaimed lines are unknown, not automatically correct controls. The current
pilot does not implement phonetic counterfactual tests, certified acoustic
endpoints, automatic application, final render review or an agent API.

## Directed timing investigation

Add `--acoustic-timing` to a listening run to investigate supported/proposed
text with the existing Spanish CTC model and native-mix periodicity tool.
Unresolved text is routed back to listening instead of forced alignment.
The agent performs two bounded alignments, checks that prefix anchors still
refer to the same occurrence, and checks boundary stability. Neighboring lines
limit search windows; overlapping lines, unchanged contexts and unstable
alignments produce explicit abstentions. A stable CTC path is still a
hypothesis, not a measured probability of correctness.

After stable alignment, the agent investigates periodicity around the final
word. It keeps the aligned word span separate from a periodicity-end interval.
If periodicity ends substantially before the aligned word, it requests review
of the final consonant/overlapping voice instead of proposing an early cut.
Periodicity is neither a lead-voice identifier nor a phonetic end certificate.

For a local acoustic connectivity test without recognition-provider credentials:

```sh
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
python3 lyricgen/backend/scripts/run_active_audio_review.py \
  --snapshot .context/pilot/snapshot.json --audio .context/pilot/original.wav \
  --output .context/pilot/timing-01 --timing-only --line-indices 0 3 --execute
```

This explicitly conditions on the supplied text. It cannot establish that the
text was sung. Offline mode requires the existing CTC weights in local cache.
The mode runs no recognition-provider calls and processes at most six selected
lines. It does not write corrected text or timestamps to the product.

A real offline probe of two historical `Mosca De Bar` lines ran successfully:
one CTC alignment was stable across contexts (word span 1.10–1.96 seconds),
while periodicity ended at 1.71–1.72 seconds; the other line had no room for a
distinct context within neighbor constraints. The first result motivated the
explicit final-consonant/periodicity distinction above. Neither result proves
an improved timing or human-level accuracy. Timing investigation has eleven
additional isolated tests, including this observed boundary discrepancy.

## Initial local validation (2026-11-17)

Eleven isolated functional tests cover independence, changed acoustic context,
phrase leakage, uncertainty, timestamp offsets, bounded work, source identity,
provider failures and immutable input. These tests use controlled responses.

A real-file connectivity run used the historical audio of `Mosca De Bar`
(247.107 seconds), with its hash checked against the approved-corrections
benchmark and 58 source segments reconstructed from historical pairs. This is
an experiment snapshot, not a live edit target; its audio revision identifier
is local and its segment coverage may be incomplete.

The available local OpenAI credential returned `401 invalid_api_key`; Vertex
credentials were absent. Therefore this run analyzed **zero** seconds, made no
proposals, and cannot establish any recognition improvement. Its artifacts are
private under `.context/active-audio-pilot/`. This failure motivated the
per-provider authentication/quota circuit breaker. Run the same command with
valid existing credentials in the appropriate Genly environment to proceed.
