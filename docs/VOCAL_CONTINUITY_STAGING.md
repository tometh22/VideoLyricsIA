# Vocal endpoint pilot — staging 1.1.160

The final acoustic post-pass extends fresh machine-timed cards only when the
clock-verified vocal stem has both a stable spectral/energy release and periodic
evidence entirely inside the extension. It runs after current staging's retry
and display-segmentation owners, before final quality evaluation and the immutable
editor snapshot. It changes no words or provider evidence.

`VOCAL_CONTINUITY_MODE=apply` enables application only in `staging`, `development`
or `test`. Default is `off`; `shadow` records proposals. Production and unknown
environments refuse application even if the flag is set. Existing approvals,
locks, stale input fingerprints, overlapping cards, changed audio/revisions, and
an editor opened during processing all veto application. Re-measurement does not
extend twice. The legacy pitch extension is suppressed while this pilot is active.

Apply on staging API, ShortWorker and BatchShortWorker (inline, interactive and
batch transcription). No database migration. Store the exact source/release SHA
and previous flag values before rollout. Wait for empty started queues; deploy
one release, then verify the same SHA and effective mode on all three services.
Rollback: set `VOCAL_CONTINUITY_MODE=off` on those staging services and redeploy
them sequentially after verifying no active work. This does not rewrite completed
jobs or approved deliveries.

Evidence before this release: 613 comparable historical endpoints / 33 songs,
20 improvements >50ms, no regressions >50ms, 247 unchanged-reference controls
preserved. This is a reused regression set, not a new independent validation.
Ten other proposed changes lacked comparable references and were not scored.
The recorded 735ms false extension is vetoed for absent reliable periodicity.

New-upload tests exercise API → queued worker and inline fallback → database →
editor snapshot → polling with real acoustic extraction on synthetic audio,
using local ASR/storage doubles. They do not assert new ASR accuracy.
