# UMG corrections: review, render, publish

## Operator workflow

1. Analyze the request; compare the proposal with the saved lyrics. Apply selected operations or edit manually.
2. From Changes, open **Revisar y confirmar render**: the original request and complete saved lyrics appear together. Confirm **Aprobar y re-renderizar**. The editor uses the same durable approval and render endpoint.
3. Stay in Changes while the render runs. Once finished, review the resulting video. Prepare its professional master if required.
4. Confirm **Publicar actualización**. This performs final video approval through the existing QC/billing gates, then publishes to the request's original Argentina/Chile portal. Only the ONE request selected in Changes is resolved, and only when the published content actually changed; other open requests on the same delivery stay open. The published revision is reported.
5. Alternatively **Marcar como resuelto** closes the request visibly in the portal without implying a render or publication. A reason is required (the client sees it in the portal): the API answers 422 `resolution_reason_required` without one. Resolved requests can be reopened; a reopened request sorts first in the admin list (ordered by latest lifecycle change, not by submission date).

Approval of lyrics does not publish. Publication requires the same editor revision and render fingerprint the operator reviewed. A newer save requires another review/render; double-clicks use the transactional outbox's idempotency controls.

Approved background edits: the editor now honors the same platform-admin permission as Changes (done/rejected as well as pending_review), including AI, library and uploaded backgrounds. Other roles keep their existing permissions. Multi-scene jobs must use the scene editor; neither route can overwrite the timeline with one background. Generation remains an explicit action and does not resolve/publish the request.

## Publication modes: frozen copy vs pointer to the newest render

`PUBLISH_LATEST_POINTER` (default off) chooses how publishing treats files:

- **Off (snapshot mode, the default):** every publication copies the files into
  immutable `.published-<uuid>` objects (see "File safety"). Safe against
  overwrites, but each publication stores several GB per portal and takes
  minutes.
- **On (pointer mode):** publishing copies nothing and leaves
  `published_file_keys` NULL, so the portal serves the job's current render
  directly. What the client sees in the meantime is decided by the delivery's
  visibility (below): by default the delivery is hidden while it has unpublished
  changes and appears when it is published. Publish registers the new revision, resets the
  client's approval and resolves the reviewed request, but as a few database
  writes. `pin_legacy_deliveries` does nothing in this mode (nothing is frozen
  before a re-render). A delivery that already has a snapshot keeps serving it
  until its next publication, which clears the pointer. The broadcast ProRes
  masters (`umg_master`, `umg_short`) are hidden from the portal while the
  delivery is marked in flight (`editing` / `prores_pending`), because R2 keeps
  the previous master until the new one is transcoded.
- **Client visibility** (`deliveries.client_visibility`, Admin > Cambios UMG >
  "Qué ve el cliente"): `auto` (NULL, default) hides the delivery's files while an
  edit is in flight (`stale_reason` editing / prores_pending) and no snapshot exists
  (a dead `edit_failed` is not hidden), `visible` keeps the newest cut
  available, `hidden` keeps it out until changed. The portal listing keeps the
  card and the client's requests with `files: []` and `files_hidden: true`. The
  value sits on the shared portal row because the portal is served by the
  production backend: the rule only takes effect for clients once that backend
  runs this code. Signed links already handed out stay valid until they expire.
- The publication mode can be switched from the admin panel (`/admin/publication-settings`,
  stored in the local `system_settings` table, super admin only);
  `PUBLISH_LATEST_POINTER` is only the default when nobody has chosen.
- Unreferenced `.published-*` and `.vN` objects are not deleted by this change.
- APIs expose the mode (`publication_mode` on `/admin/change-requests` and the
  campaign pipeline) so the UI does not claim "the portal still shows the old
  cut" when it does not.

## File safety

`deliveries.published_file_keys` points to immutable server-side copies. A successful publication switches the database pointer only after every required copy succeeds. Argentina and Chile have independent pointers. A failed copy leaves the previous publication and open request intact.

Legacy deliveries are pinned before working files are overwritten (render uploads and professional-master uploads). Storage outages fail closed. Known-missing legacy files remain unavailable rather than falling through to a newer working file. This cannot retroactively freeze URLs already issued for mutable keys, nor undo files overwritten before this deployment.

Legacy render detection uses a successful job plus an archived overwrite after both the saved document and the request, not the proposal's `applied` status. New partial renders record their exact editor revision and completion time.

Campaign publication uses the same immutable-file contract. Batch counters are computed after flushing item transitions; old `partial` operations with all items sent are reported as completed. Completion refreshes the campaign history. Republishing a corrected campaign cut does NOT resolve any client request (decision of 2026-09-18, `6af323c1`): a campaign send attests the selected cut, not every instruction that preceded it, so the requests stay open until closed from Changes ("Publicar actualización" for the reviewed one, or "Marcar como resuelto"). A failed song no longer aborts the operation: it is reported per song (`error_code`, `error_detail`, `retryable`), a stalled operation can be retried (`POST /batch/delivery-operations/{id}/retry`) and the reconciler re-queues one that lost its worker.

## Deployment requirements (not executed by this change)

- Sole release owner must coordinate the shared portal schema and both environments. Staging's `DELIVERIES_DATABASE_URL` points to the live portal DB; a staging-only migration is insufficient.
- Apply additive migration `a7b9c1d3e5f7` to the actual shared deliveries DB **before** starting any API/worker with this model. No destructive migration or bulk request-resolution backfill is needed.
- Before deploying change-request timestamp tracking, also add `delivery_change_requests.updated_at` to the shared deliveries DB with migration `b8c0d2e4f6a8`.
- Deploy the snapshot-aware portal API as well as rendering/publishing APIs and workers. An older portal API ignores the snapshot pointer; do not advertise publication isolation while any serving portal API is old.
- Release `VERSION`, frontend `package.json`/lock and `CHANGELOG.md` together, run clean-checkout CI, then merge/deploy under one owner.
- Smoke with synthetic Argentina and Chile deliveries: publish A, render B, verify portal still serves A, explicitly publish B, verify revision/request and downloaded bytes. Include a failed snapshot, concurrent newer edit, manual resolution, and campaign resend. Do not approve/publish customer videos as smoke tests without specific authorization.
- Rollback: keep the additive column and pinned files. Reverting to a portal API that ignores pointers removes publication isolation; coordinate that risk explicitly. Do not delete snapshot objects during rollback.

## Read-only incident evidence

On 2026-09-18, request 85 had a completed partial render but a legacy delivery with null fingerprint/stale fields; the applied proposal fallback incorrectly returned to the editor. Its previously mis-targeted proposal must not be reapplied blindly.

Campaign operation `53432883-055f-49ff-a15f-e1236554d772` had one `sent` item but status `partial`. Ojitos Verdes (`e3d01a71ae49`, Chile delivery 307) was already published as revision 2: its published fingerprint matched the current staging render. No customer render/publication/resolution was performed during diagnosis.

At 02:13 UTC, both concurrently submitted edits had completed successfully: Borracho Y Agresivo (`396c71f0b66b`, 02:07:19) and De Coquimbo Soy (`02dba655d75a`, 02:12:35). The latter started on the worker only at 02:07:19, then spent several minutes archiving/uploading after 90%. The existing progress screen does not distinguish queued work from execution. These actual customer jobs were only inspected, never restarted or approved by the agent.

## Aviso por mail a UMG al publicar una corrección

Apagado por defecto y sin destinatarios. Para activarlo (decisión del dueño, por entorno):

1. `UMG_PUBLISH_NOTIFY_ENABLED=1`.
2. Las casillas de cada portal: `UMG_PUBLISH_NOTIFY_RECIPIENTS_ARGENTINA` y `UMG_PUBLISH_NOTIFY_RECIPIENTS_CHILE`
   (o una sola lista en `UMG_PUBLISH_NOTIFY_RECIPIENTS`), separadas por coma; máximo 10 válidas.
3. En un entorno que no sea producción (el trabajo gestionado de UMG corre en staging), esas direcciones tienen que estar
   además en `EMAIL_STAGING_ALLOWLIST`; si no, el mail se descarta o se redirige. Es la barrera que evita escribirle a
   un cliente real desde staging por accidente.

Cuándo avisa: solo una publicación que cerró pedidos del cliente (`resolved_change_requests` no vacío), con contenido nuevo
y con los archivos visibles para el cliente (no oculta). Un reenvío del mismo corte o una entrega oculta no avisan.
Qué dice: la versión, el portal, lo que pidieron (sus propias palabras, hasta 5 y 240 caracteres) y un botón al portal.
Un fallo de envío nunca interrumpe la publicación.
