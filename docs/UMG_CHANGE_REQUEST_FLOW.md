# UMG corrections: review, render, publish

## Operator workflow

1. Analyze the request; compare the proposal with the saved lyrics. Apply selected operations or edit manually.
2. From Changes, open **Revisar y confirmar render**: the original request and complete saved lyrics appear together. Confirm **Aprobar y re-renderizar**. The editor uses the same durable approval and render endpoint.
3. Stay in Changes while the render runs. Once finished, review the resulting video. Prepare its professional master if required.
4. Confirm **Publicar actualización**. This performs final video approval through the existing QC/billing gates, then publishes to the request's original Argentina/Chile portal. The selected request is resolved and the published revision is reported.
5. Alternatively **Marcar como resuelto** closes the request visibly in the portal without implying a render or publication. A response is optional. Resolved requests can be reopened.

Approval of lyrics does not publish. Publication requires the same editor revision and render fingerprint the operator reviewed. A newer save requires another review/render; double-clicks use the transactional outbox's idempotency controls.

Approved background edits: the editor now honors the same platform-admin permission as Changes (done/rejected as well as pending_review), including AI, library and uploaded backgrounds. Other roles keep their existing permissions. Multi-scene jobs must use the scene editor; neither route can overwrite the timeline with one background. Generation remains an explicit action and does not resolve/publish the request.

## File safety

`deliveries.published_file_keys` points to immutable server-side copies. A successful publication switches the database pointer only after every required copy succeeds. Argentina and Chile have independent pointers. A failed copy leaves the previous publication and open request intact.

Legacy deliveries are pinned before working files are overwritten (render uploads and professional-master uploads). Storage outages fail closed. Known-missing legacy files remain unavailable rather than falling through to a newer working file. This cannot retroactively freeze URLs already issued for mutable keys, nor undo files overwritten before this deployment.

Legacy render detection uses a successful job plus an archived overwrite after both the saved document and the request, not the proposal's `applied` status. New partial renders record their exact editor revision and completion time.

Campaign publication uses the same immutable-file contract. Batch counters are computed after flushing item transitions; old `partial` operations with all items sent are reported as completed. Completion refreshes the campaign history. Republishing a corrected campaign cut resolves older requests for that delivery; requests newer than the cut stay open.

## Deployment requirements (not executed by this change)

- Sole release owner must coordinate the shared portal schema and both environments. Staging's `DELIVERIES_DATABASE_URL` points to the live portal DB; a staging-only migration is insufficient.
- Apply additive migration `a7b9c1d3e5f7` to the actual shared deliveries DB **before** starting any API/worker with this model. No destructive migration or bulk request-resolution backfill is needed.
- Deploy the snapshot-aware portal API as well as rendering/publishing APIs and workers. An older portal API ignores the snapshot pointer; do not advertise publication isolation while any serving portal API is old.
- Release `VERSION`, frontend `package.json`/lock and `CHANGELOG.md` together, run clean-checkout CI, then merge/deploy under one owner.
- Smoke with synthetic Argentina and Chile deliveries: publish A, render B, verify portal still serves A, explicitly publish B, verify revision/request and downloaded bytes. Include a failed snapshot, concurrent newer edit, manual resolution, and campaign resend. Do not approve/publish customer videos as smoke tests without specific authorization.
- Rollback: keep the additive column and pinned files. Reverting to a portal API that ignores pointers removes publication isolation; coordinate that risk explicitly. Do not delete snapshot objects during rollback.

## Read-only incident evidence

On 2026-09-18, request 85 had a completed partial render but a legacy delivery with null fingerprint/stale fields; the applied proposal fallback incorrectly returned to the editor. Its previously mis-targeted proposal must not be reapplied blindly.

Campaign operation `53432883-055f-49ff-a15f-e1236554d772` had one `sent` item but status `partial`. Ojitos Verdes (`e3d01a71ae49`, Chile delivery 307) was already published as revision 2: its published fingerprint matched the current staging render. No customer render/publication/resolution was performed during diagnosis.

At 02:13 UTC, both concurrently submitted edits had completed successfully: Borracho Y Agresivo (`396c71f0b66b`, 02:07:19) and De Coquimbo Soy (`02dba655d75a`, 02:12:35). The latter started on the worker only at 02:07:19, then spent several minutes archiving/uploading after 90%. The existing progress screen does not distinguish queued work from execution. These actual customer jobs were only inspected, never restarted or approved by the agent.
