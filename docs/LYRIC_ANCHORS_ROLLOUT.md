# Lyric-anchor background rollout

`BG_LYRIC_ANCHORS` controls the two-step planner used by **Inspired by lyrics**.
It does not rewrite literal or improved operator prompts and it does not change
Auto mode.

## Modes

- `off` (default): keep the legacy one-pass planner.
- `shadow`: extract and record verified lyric anchors, but keep the legacy
  prompt and rendered output.
- `on`: extract verified anchors first, then compose the production prompt from
  those anchors and enforce the coverage gate.

Invalid or missing values resolve to `off`. The runtime rollout fingerprint
includes `shadow` and `on`, so API/worker disagreement fails before generation.
The historical fingerprint remains byte-identical while the flag is `off`,
allowing a code-only rolling deploy to drain older queued work.

## Staging activation

1. Deploy the exact release SHA with the flag absent or `off` and wait for API,
   Worker, ShortWorker, BatchWorker, BatchShortWorker and quality-worker to
   report that SHA. Do not combine this with another rolling deployment.
2. Pause new submissions and wait until interactive and `batch_render` work is
   empty. A flag change while mixed replicas are alive is expected to reject
   mismatched jobs; it must never silently generate under the wrong planner.
3. Set `BG_LYRIC_ANCHORS=on` with deploys skipped on every code service, then
   redeploy the services sequentially. Keep submissions paused until the fleet
   is coherent again.
4. Run one synthetic approved campaign item in Inspired-by-lyrics mode. Retain
   the extracted anchors, composed prompt, provider receipt and output hash.
   Confirm the prompt uses at least four renderable lyric anchors and includes
   the fixed composition requirements (full 16:9, clean lyric area, stable
   light, no legible text/logos and no unintended scene/camera transition).
5. Reopen submissions only after the canary passes. Do not use a real campaign
   or alter a human-approved transcript for the smoke test.

## Rollback

Pause submissions, set `BG_LYRIC_ANCHORS=off` on every code service with deploys
skipped, redeploy sequentially and wait for coherent readiness before reopening.
No migration or cache deletion is required; anchor mode participates in the
background cache namespace and the API/worker rollout fingerprint.
