# Campaign creative direction and contractual records

Available for lyric-video campaigns under the existing batch feature/tenant access gate.

## Operator workflow

1. Open a campaign → **Estilo y fondos**. Select individual songs, all search matches or all songs across pages.
2. Create named groups and choose percentage or count. Enable only the fields to change; other settings and source lyrics remain intact. Saved groups can be reused for a later allocation.
3. For a contractual allocation, record the agreement/reference, universe and rounding acceptance. With 39 songs, 50/50 requires an explicitly accepted 20/19 or 19/20 split. The original percentages remain recorded.
4. Preview each assignment and its changes before saving. Saving does not call providers or enqueue renders. Open reviews, later changes and progressed jobs cause conflicts instead of overwrites. Pinned individual exceptions are preserved unless explicitly included.
5. Review each song's lyrics/timing in the existing editor. Assigned visual choices reopen there; visual changes autosave as pinned exceptions, with errors visible and a flush before exiting or approving.
6. From **Estilo y fondos**, generate selected songs whose lyrics/timing have human approval. This uses native `/generate`, quota/capacity checks and transactional outbox. If the queue capacity is reached, already submitted jobs remain visible; the remaining approved songs can be selected in a later pass.
7. **Historial de videos** shows this campaign's generations and related variants, with links to each video's native review and version history.
8. **Contrato y cumplimiento** compares targets, assignments, generated, approved, verified and manually registered deliveries. Export CSV, Excel or use **Imprimir / guardar PDF**. Excel includes all assignments and individual change rows, including songs not rendered yet.

## Contractual evidence

- Fixed photo + effect requires an image source, explicit photo movement, an actual overlay effect and no AI image animation/scenes/variation. `foto_viva` is not accepted as a fixed-photo effect.
- Veo groups store the exact configured model identifier. Veo Lite is `veo-3.1-lite-generate-001` (Vertex public preview), distinct from the ordinary default `veo-3.1-fast-generate-001`. Choose the model per group; the frozen receipt controls the provider, cache key and provenance without changing other campaigns. Provider access/capacity errors remain visible and never certify another model as Lite.
- Successful FFmpeg renders record final video/background SHA-256, source type, applied effect and renderer. Provider evidence is bound to exact background bytes and the owning job's provenance row. Requested settings alone never certify a render.
- A same-job retry may reuse prior provenance only for identical background bytes. Missing cache provenance, stale sidecars, fallback renderers and degraded generation remain unverified or deviations. Native per-job provenance retains the original prompt referenced by its ID/hash.
- One principal song delivery is the counting unit. Re-renders and variants remain visible but do not increase the contractual universe. Delivery is an explicit record of a current, approved file and destination; clicking it does not send a message or upload elsewhere.
- Current assignments live in existing JSON columns. Append-only audit rows preserve operations, rendering evidence and deliveries. No schema migration is required. Creative reports omit the private source-lyrics manifest.

## Validation and release

New backend coverage exercises apportionment, authorization, idempotency, stale previews, locks, exceptions, round-trip generation through native human approval/outbox, variant isolation, delivery hashes, spreadsheet exports and an actual synthetic FFmpeg photo/effect render. Frontend coverage includes selection across 39 songs, partial changes, save/preview separation, serialization of visual autosave and browser persistence/history.

Ship through protected staging CI only. Confirm idle queues and one deployment owner. Roll back with a protected revert PR of the 1.1.14 merge commit (`git revert -m 1 <merge-sha>`), or disable batch access while investigating. Existing JSON/audit records are additive and remain readable after rollback; do not remove them. Do not assign styles or approve/generate the real Chile campaign as part of the code deployment.
