# Spreadsheet references in lyric campaigns

The campaign manifest accepts optional `source_reference` records bound to the
asset ID and audio SHA-256. A candidate includes its original sheet text, text
hash, document URL/ID, tab and row numbers. An absent record explicitly records
that no matching external lyric was found. Neither state is human approval.

Upload while the new campaign is paused. Manifest replays preserve the source;
conflicting replacements return 409. Generic item patches cannot replace this
contract. Existing manifests without sources keep their previous behavior.

The source travels through the durable transcription outbox and queue. ASR and
the complete-audio hypothesis remain independent of catalogue text. Afterwards,
the worker uses the existing audio/reference attestation gate. Only studio
references admitted for whole-song alignment enter the aligner; live or
unattested candidates remain available for manual consultation. Alignment
failure retains the audio transcript. Accepted text has `catalog_reference`
provenance and never masquerades as operator or independent-provider output.

The editor shows the original sheet source separately from the audio hypothesis,
including whether it was actually applied. Every song still requires human
lyrics/timing approval before background or render work.

## Staging release and rollback

Release 1.1.11 is additive, requires no migration, and is opted into by source
records on new items. Deploy one coherent API/worker release before registering
source-bearing manifests. `/batch/campaigns/access` advertises
`source_reference_schema=1`. Verify that the served frontend has the source
panel, then run a bounded ten-song pilot in the separate Chile campaign.

Rollback: PATCH only the new campaign to `status=paused` to stop promotions,
then let already accepted jobs drain. Do not interrupt human edits or touch the
Argentina campaign. If code must be reverted, submit a protected PR reverting
the recorded release merge commit and pass clean-checkout CI before deployment;
do not send source-bearing queue payloads to the older worker signature. Keep
all audio, source records, machine evidence and human edits. A paused campaign
cannot be handed to the reviewer: resume after recovery.

Validation covers manifest transport/idempotency, audio/text binding, rejected
and missing references, alignment failure, provider provenance and the separate
review panel. The real pilot measures operational behavior; fixture tests do
not establish transcription accuracy.
