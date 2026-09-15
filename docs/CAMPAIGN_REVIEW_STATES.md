# Campaign review states

Campaign totals are exclusive: pending + approved + discarded = all songs.
The saved-human-changes filter (`scope=drafts` for existing links) is a subset
of pending songs, not another category to add to the total. Approval still
requires the existing explicit action; this filter makes no quality claim.

`EditorDocument.updated_by`, a `manual` checkpoint, or a personal account name
alone does not prove interactive review. Batch maintenance has used all three.
The queue requires a material-change audit associated with server-recorded
`editor_activity_heartbeat` evidence for the same job and actor, within 45 seconds,
and at the edit's source or destination revision. The heartbeat endpoint requires
an active editor lock and assigns its timestamp on the server. This is operational
evidence of editor activity, not proof of listening or correctness.

For older diffs without revision metadata, an explicit nearby editor version
and matching activity are required too. Opening the editor without a material
change does not qualify. Missing history or telemetry remains **without recorded
human changes**; it must not be described as definitely untouched. A newly saved
edit can appear after the next activity heartbeat and queue refresh.

The known batch/shared account `batch-universal-staging` cannot establish personal
review attribution. Additional automation accounts are explicitly registered in
server environment variable `REVIEW_AUTOMATION_USERNAMES` (comma-separated names,
case-insensitive, additive to the known account). Username prefixes are not used
to guess whether an account is automated. Scripts using personal credentials do
not qualify merely because of the account name; activity binding is still needed.

Reads select metadata only and never modify documents, approvals or history.
The additive `editor.review_saved` receipt preserves material structural changes
and fast drafts even when no full version snapshot is created. The original
save transaction owns that receipt. Old API consumers retain `is_draft` and the
`drafts` counter, now derived from the same stricter evidence as the new UI.
