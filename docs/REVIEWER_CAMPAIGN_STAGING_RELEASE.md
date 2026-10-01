# Supervised campaign candidates — staging-only release

Authorized target: existing campaign `ba3318bdfffe`, exactly 300 existing job IDs,
at `https://staging.genly.pro/campaigns/ba3318bdfffe`. No production promotion.
The release owner performs merge, deployment, publication and verification.

## Gates and isolation

- Run `make check` and clean-checkout CI on the final PR head before merge.
- Verify staging is idle, record its previous commit and feature settings, and
  preserve the source snapshot. No background/video smoke is authorized here.
- The ordinary post-push Edit Smoke creates paid transcription and visual work.
  Cancel only the run matching this staging merge SHA during its initial wait;
  verify its paid step never started. Do not disable the workflow or any required
  check. Record it as not executed by scope, not passed.
- Keep the existing PostgreSQL collaboration test and additionally run the
  supervised-candidate adoption/approval case in its disposable CI database.
  Synthetic evidence tests mechanics, not recognition accuracy.

## Staging settings

API service only:

```env
REVIEWER_ASSIST_ENABLED=1
REVIEWER_ASSIST_CAMPAIGN_ID=ba3318bdfffe
REVIEWER_ASSIST_PUBLISH_ENABLED=1
REVIEWER_ASSIST_INFERENCE_ENABLED=0
REVIEWER_CANDIDATE_STORAGE=r2
REVIEWER_TIMING_CAPTURE_ENABLED=1
REVIEWER_TIMING_CAPTURE_EPOCH=post-1284-staging-20260906
```

Inference stays explicitly off on every worker. Existing text suggestions stay
on; timing suggestions and `STABLE_PITCH_TAIL_ENABLED` remain off. Do not change
the existing `LYRIC_HOLD_S=0.5`. Empty campaign scope fails closed. Display and
publication never invoke a model, download a song or enqueue a new review.

Staging shares an R2 bucket: candidate objects must use the environment-isolated
`staging/reviewer-candidates/v1/` prefix. Private release inputs use
`staging/reviewer-release-inputs/ba3318bdfffe/`; never overwrite media objects.

## Publication

Build a private bundle using `scripts/publish_reviewer_campaign.py --build-bundle`.
It contains all 300 source-bound states and only available complete candidates.
Transfer it privately; inspect `--dry-run` inside the staging API container before
`--execute`. Compare its roster with the live existing campaign, not an imported
duplicate campaign. Execute is resumable per song, immutable per candidate and
idempotent. Preserve conflicts and active human reviews; report stale sources.

Publish first available candidates without waiting for listening to finish.
Subsequent imports may update affected status rows and add newly completed
candidates. No publication changes current lyrics/timing, protections or approval.
Adoption remains an explicit human action through the existing proposal API.

## Verification and rollback

Verify `/health/ready`, exact service commit and staging frontend commit. Log in
through the real staging UI, enter Campañas, open the existing campaign and a
candidate, play its full audio and inspect changes/doubts. Do not apply or approve
a real song to test. Compare source/approval snapshots after publication.

Immediate feature rollback (staging API only):

```sh
railway variables -e staging -s api --set REVIEWER_ASSIST_ENABLED=0 --set REVIEWER_ASSIST_PUBLISH_ENABLED=0 --set REVIEWER_TIMING_CAPTURE_ENABLED=0 --set REVIEWER_ASSIST_INFERENCE_ENABLED=0 --skip-deploys
railway redeploy -e staging -s api --yes
```

Wait for API readiness and confirm candidate display/adoption is disabled.
Keep published evidence and status records; do not delete or revert human work.
If a code rollback is required, use a protected-PR revert of the recorded staging
merge commit and clean CI; never reset shared history or promote to production.
