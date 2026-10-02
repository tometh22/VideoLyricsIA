# Changelog

All notable changes to VideoLyricsIA (GenLy AI) are documented here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.1.94] - 2026-10-01

### Added

- Optional mail to UMG when a correction they asked for is published (`umg_publication_notice.py`,
  `emails.send_umg_publication_notice`). OFF by default and with no recipients: nothing is sent until the owner sets
  `UMG_PUBLISH_NOTIFY_ENABLED=1` and `UMG_PUBLISH_NOTIFY_RECIPIENTS_ARGENTINA` / `_CHILE` (or
  `UMG_PUBLISH_NOTIFY_RECIPIENTS`; at most 10 valid addresses). Only a publication that closed client requests and
  whose files are visible to the client notifies (a plain re-send or a hidden delivery stays silent). The mail names the
  version and the portal, quotes what they asked for (escaped, capped) and links to the portal. Outside production the
  existing staging gate still applies: an address only receives from staging if it is in `EMAIL_STAGING_ALLOWLIST`.

## [1.1.92] - 2026-10-01

### Added

- `scripts/check_fleet_config_parity.py`: compares the pipeline/calibration environment (the inputs of the fleet
  runtime token) across the services of a Railway environment and exits 1 listing the keys that differ. Production
  had drifted (quality-worker missing 15 variables, `CTC_ALIGN_MIN_MED_SCORE` set on some services only), which made
  quality jobs silently discard with `runtime_identity_mismatch` and `/health` report `fleet_runtime_token_mismatch`.
  The production services were aligned the same day; run the check before every promotion.

## [1.1.91] - 2026-10-01

### Added

- Production-capable switches for the two approval gates that did not exist in the
  production line, so a promotion can keep production's behaviour until the QC redesign
  is ready: `DELIVERY_QC_GATES_OFF` (delivery QC/preflight never blocks, any environment)
  and `LANGUAGE_REVIEW_ADVISORY` (the language review warns but never answers 409 on
  approve / approve-lyrics, any environment). Both are explicit operator env vars,
  default OFF (gates stay active), and are reported in `/health` under `features`
  (`delivery_qc_gates_off`, `language_review_advisory`). The older staging-only switches
  (`DELIVERY_QC_STAGING_GATES_OFF`, `LANGUAGE_REVIEW_STAGING_ADVISORY`) keep working and
  stay inert outside staging. Reports are still generated and shown; they only stop gating.

## [1.1.90] - 2026-10-01

### Fixed

Alignment of the production line (`tometh22/umg-chile-portal`) and `main` with staging:
the hotfixes that existed only there are now in staging, ported by hand (a plain
`git merge main` would duplicate tables and leave the app unable to start).

- Portal listing (`/api/deliveries/items`, from production `ace4063b`): the handler is no
  longer `async` (it did synchronous DB/Redis/R2 work on the event loop), all database
  state is read and the session released BEFORE any Redis/R2 I/O (PostgreSQL kills idle
  transactions after about a minute), the size cache uses one `mget` and a pipelined
  `setex`, and the HEAD fan-out has a hard 12 s deadline on the bounded metadata client.
  Keeps staging's client visibility rules, `files_hidden` and revision fields.
- Transcription retries (`main` #1218): a failure by deadline is deterministic, so the
  RQ retry of a job whose previous attempt hit the deadline is discarded instead of
  re-running (and re-billing) demucs + WhisperX; transient failures still retry. Failures
  now carry an `error_code`.
- ASGI replay (`main` #1220): the replay `receive` now propagates the real client
  disconnect instead of waiting forever, which could hang SSE endpoints.
- Provenance (`main` #1219): `ai_provenance.duration_ms` is split into queue time and
  inference time (`predict_time_ms`, `queue_time_ms`). New idempotent Alembic revision
  `ba888d1665d8`, re-chained behind `e5a7c9b1d3f6` (single head); the workers wait for
  its columns at startup.
- Gap-refetch gate (production `d8c45331`): `_reference_is_live` can no longer be unbound.

## [1.1.89] - 2026-10-01

### Security

- `transformers` 4.57.6 -> 5.18.0 (exact pin). The 4.x series ended at 4.57.6, which
  is affected by CVE-2026-80047, and every fix only exists in 5.x, so this is a
  major upgrade. The 6 temporary exceptions for `transformers` are removed from
  `security_exceptions.json` (the audit gate now passes with the 9 that remain,
  all `torch`/`ecdsa`). `safetensors` lower bound raised to 0.8.0 (what 5.x needs);
  `huggingface-hub` moves to 1.x transitively.
- Validation: on the real pinned CTC model (`jonatasgrosman/wav2vec2-large-xlsr-53-spanish`
  at its immutable revision) the emissions on a fixed 20 s signal are bit-for-bit
  identical to 4.57.6 (max abs diff 0.0, same vocabulary and blank id); the 227
  alignment/LoRA/anchor tests pass; the LoRA path (`PeftModel` + the ASR pipeline with
  word timestamps + `processor.save_pretrained`) loads and runs. No code change was
  needed.

## [1.1.88] - 2026-10-01

### Added

- Per-delivery control of what the CLIENT sees (Admin > Cambios UMG, "Qué ve el
  cliente"): **Automático** (default) hides the delivery while it has unpublished
  changes and shows it again when it is published, **Siempre visible** keeps the
  newest cut available, **Oculto** keeps it out of the portal until an operator
  changes it. The portal keeps the card (and the client's requests) and only drops
  the files (`files_hidden`). Only an edit IN FLIGHT (`editing`, `prores_pending`)
  auto-hides: a frozen snapshot is never hidden and a dead edit (`edit_failed`) is
  not either, so the operator is never left without a way out. A stale
  professional master is never served, even when forced visible. Previously
  issued signed links keep working until they expire (7 days). Publishing a
  manually hidden delivery now says so ("OJO: el cliente todavía no la ve"), and a
  hidden delivery cannot have a master prepared from the portal.
- Admin switch "Publicar sin copiar archivos": picks copy-the-files or no-copies
  publication without a Railway edit or deploy. Deployment-wide, so it asks for
  confirmation and only the super admin can change it. The panel choice overrides
  `PUBLISH_LATEST_POINTER`, which remains only the default.
- Campaigns: songs the client cannot see at all (hidden in EVERY portal they are
  published to) are reported as `portal_hidden` and worded accordingly ("Oculto
  para el cliente", "el cliente no ve el video hasta que lo publiques").

### Database

- `deliveries.client_visibility VARCHAR(10)`: nullable, no default, added with
  `IF NOT EXISTS` (Alembic `e5a7c9b1d3f6`). The portal rows live in the production
  database, so the column must exist there before staging reads it; code that
  ignores it keeps working. Also a local `system_settings` table (never the shared
  portal database).

### Changed

- No-copies publication wording no longer promises the client sees the new cut as
  soon as it renders: with the automatic mode they see it when it is published.

## [1.1.87] - 2026-10-01

### Fixed

- Publishing a correction is now ONE click even when the professional (ProRes)
  master is missing: "Publicar en el portal y dar por resuelto" starts the master,
  shows its progress, asks the server again by itself and publishes the same
  reviewed cut when it is ready (15 minutes maximum, then it hands control back
  with a clear message). The button stays disabled while it waits, and the old
  "Preparar el archivo profesional" step is now a small "Solo preparar" link and
  "Elegir formato y publicar" for legacy deliveries without a saved format.
- A second click can no longer start a second multi-GB transcode: operator-driven
  prewarm requests (enable-prores, publish, portal prepare, lazy download) return
  the live job instead of enqueueing again. The edit pipeline keeps its re-run.
- After a successful publication the screen no longer replaces "Publicada la
  versión N" with "preparation finished, this does not publish".

- Campaigns: deliveries published before fingerprints existed (legacy rows) were
  never flagged as outdated, so a corrected video looked up to date under
  "Entregada". They are now detected with the same evidence the admin screen uses
  (the job was overwritten after the portal last received it).

### Changed

- Songs with a cut newer than the portal's are split by what the operator must do
  next: "Listas para reenviar" (approved, only need sending) and "Corte nuevo sin
  aprobar" (approve first). The banner says how many of each and links to each
  list, the filter has both options, every song carries a matching mark
  ("Listo para reenviar" / "Falta aprobar el corte nuevo"), and the Entregada tab
  shows how many of its songs need resending. The pipeline flags expose
  `resend_ready` and `resend_review`.

## [1.1.86] - 2026-09-30

### Changed

- Admin > Cambios UMG shows a three-step correction flow (Corregir, Generar el
  video nuevo, Publicar) with ONE primary button per state named after what it
  does ("Corregir en el editor", "Generar el video corregido", "Publicar en el
  portal y dar por resuelto"). AI suggestions become an optional helper,
  closing without publishing is a small collapsed link that still requires a
  reason, and publication errors explain what happened and what to do instead
  of the generic "El servidor no aceptó la publicación". QC/preflight wording
  only appears when the gate is actually blocking.
- The client change inbox in campaigns shows the same three steps per request.
- Wording about what the client sees follows `publication_mode`: it no longer
  says "the portal keeps the previous cut" when the portal serves the newest
  render, and the campaign banner and chips are decided per song.
- Review fixes: Ctrl/Cmd+Enter on a focused secondary button no longer runs the
  primary action (publishing); the "render submitted" notice picks its wording
  after the publication mode is known; a confirmed portal send forgets its
  idempotency key and a deduplicated one says so; a retry the server refuses is
  explained in words and only offered once the send stopped or stalled; an open
  close-form is not inherited by another request.

## [1.1.85] - 2026-09-30

### Fixed

- Campaign portal sends: a song that raises no longer stops the songs after it
  (each song is isolated, processed in a stable order, and a failure marked
  earlier is no longer rolled back by a later crash); `stale_approval` is not
  offered a retry that can never succeed; an unreachable Redis is no longer read
  as "no job" (no re-queue on a guess); the stalled-send sweeper skips locked
  operations and moves on from ones it cannot re-queue; the list of requests a
  send may close is capped.
- A client request can only be closed by a publication if it was submitted
  before the editor save that holds the fix and before the render finished, and
  the job has an editor document; the inbox uses the same rule.
- The change-request notification subject can no longer be broken by a newline
  in the artist or song.
- An accepted `/edit` now marks the job's deliveries as changing immediately, so
  the old broadcast master and the client's approval do not look current while a
  re-render is in flight. The portal hides a stale ProRes master for any row
  marked stale (any reason), independent of the publication mode, because
  staging and production share the deliveries database.
- A job whose R2 files an active portal delivery without a snapshot still
  serves can no longer be deleted (single and bulk); a clear 409 explains why.
- `portal_serves_latest` is decided per delivery (a frozen snapshot keeps
  serving the old cut) instead of by the global publication flag.

## [1.1.84] - 2026-09-30

### Added

- `PUBLISH_LATEST_POINTER` (off by default): publish to a client portal without
  copying any file. The portal then serves the job's newest render, publishing
  becomes a few database writes instead of minutes and several GB of copies per
  portal, and nothing is frozen before a re-render. ProRes masters stay hidden
  while a re-render is in flight so the client never gets the old master next
  to a new MP4. Existing snapshots are untouched until their next publication.
- The campaign pipeline and `/admin/change-requests` report `publication_mode`;
  the campaign UI says "corte nuevo sin registrar" instead of "portal
  desactualizado" when the portal already serves the newest render.

## [1.1.83] - 2026-09-30

### Added

- Campaigns get a "Cambios del cliente" view with the client change requests of
  that campaign (portal, step, wait against a 48 business hour goal, the
  client's own approval), a banner with how many songs have a cut newer than
  the client portal, and "Cerrar con nota" for the campaign owner and admins.
- A campaign portal send can close the client requests the operator ticks
  (and only those), with a note the client reads. Every id is validated with
  one rule at send time and again by the worker; nothing is closed by default.
  New local column `delivery_batch_items.change_request_intent` (additive
  migration `d3f5a7c9e1b4`, also required by the worker schema gate).
- Everything is behind `CAMPAIGN_CHANGE_REQUESTS_ENABLED` and
  `CAMPAIGN_CHANGE_REQUEST_ACTIONS`, both off by default.

## [1.1.82] - 2026-09-30

### Added

- Staging-only kill switches so corrections can be published while delivery
  QC is being redesigned: `DELIVERY_QC_STAGING_GATES_OFF=1` stops the delivery
  QC/preflight gate from blocking approval and publication, and
  `LANGUAGE_REVIEW_STAGING_ADVISORY=1` stops the language review from returning
  `language_review_unresolved` on approve and approve-lyrics. Both are inert
  unless `ENVIRONMENT` is exactly `staging` and default to off; reports are still
  generated and shown.

## [1.1.81] - 2026-09-30

### Fixed

- Regenerating a background from the editor opened with "Editar letra" in
  Cambios no longer answers 409: the proposal fence only applies when the
  request carries a proposal, operation or preview hash. The edit idempotency
  key now includes the edit type and a per-submit nonce.
- A campaign send to a client portal fails per song instead of aborting: an
  ambiguous replacement or an unexpected error marks only that song, the
  operation ends as partial, can be retried (`POST /delivery-operations/{id}/retry`)
  and the reconciler re-queues one that lost its worker. The backend now
  enforces the campaign's fixed portal and audits each published song.
- The client change-request notification names the portal (Argentina/Chile),
  links straight to the request and can also reach the campaign owner
  (`CHANGE_REQUEST_NOTIFY_OWNER`, off by default).
- Admin > Cambios orders by the latest lifecycle change so a reopened request
  is no longer pushed below the list cut.

### Added

- Campaign cards show how many client change requests are open and how long
  the oldest has waited.

## [1.1.80] - 2026-09-30

### Fixed

- Hide content hashes stored as technical codes in campaign lists and the song
  drawer header instead of showing them as if they were catalog codes.

## [1.1.79] - 2026-09-30

### Added

- UMG change requests written in free prose are now turned into concrete,
  reviewable lyric changes. The strict parser only understood quoted
  "cambiar X por Y" and converted 4 of 77 instructions in requests
  #112-#131; a model (Gemini 2.5 Pro via Vertex) now reads the comment with
  the numbered lines and proposes rewrites, line splits/joins and timing
  moves. A deterministic validator only offers what the client wrote: any
  word that enters or leaves must be in the comment, the change must fall
  near a cited time, and timing comes from the heard words, never from the
  model. On the 20 requests: 136 applicable changes instead of 4, manual
  items 73 -> 17; where UMG wrote the exact text it wanted, the proposal
  contains it in 86 of 95 items (the operators' saved lyrics: 56). Nothing
  is applied automatically: the admin request panel shows each change with
  the client's words and applies the selected ones. The interpretation runs
  in the background (~1 min, status "interpreting") behind
  `CHANGE_REQUEST_INTERPRETER_ENABLED`; a model failure leaves the request as
  before.

### Fixed

- Revisión rápida no longer proposes a single automatic ear's text as a fix.
  On staging "El Cigarrito" it offered "pito → pido" (Chilean slang) and
  "de a vara → de habara". Measured over 298 approved songs, single-ear
  points were ~1 false alarm per song and, on the 136 UMG change requests,
  found the right place 48 times but the right text only 6. They now read
  "Escuchá este tramo": the line stays as is, the doubtful words are
  underlined, the ear's version goes in the reason, and Enter plays the
  audio instead of applying anything.
- Differences that are the same sound cut differently ("a vara"/"habara")
  are no longer raised unless the official lyrics say so.
- Timing suggestions left the panel (52 alerts, 1 real request); timing is
  reviewed in "Ajustar tiempos".
- When nothing blocks approval, suggestions stay behind "Ver N sugerencias
  (opcional)" instead of taking the main card.

## [1.1.78] - 2026-09-30

### Fixed

- Add a short-lived, exact job allowlist for staging UMG manual review during
  urgent Chile corrections. The existing Argentina campaign preflight bypass
  remains separate; Chile still requires a fresh report and objective checks.

## [1.1.77] - 2026-09-30

### Fixed

- Track the latest change request update through submission, resolution, and
  reopening, and show its date and time in the admin queue.

## [1.1.76] - 2026-09-30

### Added

- Add a static Art Track preset for catalog deliveries: cover on the left,
  title and artist on the right, a blurred cover background, and no waveform.
  Keep the animated waveform preset available as a separate option.
- Expose the preset in single-song uploads, campaigns, and the Art Track editor,
  with high-quality ProRes export and individual campaign master downloads.
- **Revisión rápida** in the lyrics editor: one panel lists everything that
  should be decided before approving, each with a one-click fix, the audio
  of that moment and a preview of how the line will read. Built from the 20
  UMG change requests of 2026-09-29 (136 items, Argentina and Chile), where
  the dominant error was a machine mishearing that passed review untouched.
  Sources, all already stored or cached (no new paid calls):
  - words heard by the machine and the whole-song witness that the lyrics
    lost ("Falta texto", e.g. "dormite ya", the leading "Que");
  - where two independent ears (Gemini without reference, the witness,
    official lyrics) agree against the screen ("Se escucha distinto"),
    with alternatives when they disagree;
  - official lyrics from the campaign sheet, the lrclib cache (fetched in
    the background when missing) or pasted by the operator, used only for
    comparison;
  - corrections already made in other songs of the same artist;
  - UMG style rules: question marks on non-questions and exclamations,
    title spelling, inconsistent accents, joined words and missing spaces,
    a lonely word on screen;
  - a chorus corrected in only some of its repetitions (suggestion, since
    the corrected copy may itself carry a typo).
  Identical fixes are grouped ("se corrigen juntas" across every chorus).
  Only points with two agreeing sources or an objective rule block
  approval (409 `lyric_review_pending` on /generate, /edit and campaign
  lyric approval); single-source points are non-blocking suggestions.
  Keyboard: A apply, N keep, E listen, J/K move. Decisions are stored on
  the line (`qa_dismissed`). Songs where the ears fail are flagged as
  difficult. Backtest: 102 of the 136 requested changes had a point in the
  panel; over 313 approved staging songs 77 % would have no blocking point
  (p90 2, max 5).
- `docs/UMG_GUIA_ESTILO_LETRAS.md`: UMG lyric style guide derived from the
  131 portal change requests, also reachable from the panel.

- **Campañas as a pipeline**: every campaign screen counts the same unit
  (the song) with one stage per song — Audio, Letra, Lista, Generando,
  QC video, Aprobada, Entregada (+ Atención, Descartadas) — from a new
  read-only `GET /batch/campaigns/{id}/pipeline`; the campaign list carries
  the same counts through bulk queries. Variants never add songs and
  "Entregada" is decided by the current cut published in a portal.
- Campaign workspace: pipeline bar, one song table with one primary action
  per row, song drawer (history, versions, portal, metadata), floating bulk
  bar (generate, send, assign style, discard/restore, retry), QC focus
  player with "Aprobar y siguiente" and keyboard shortcuts, 3-step creation
  wizard, browser folder upload (resumable, SHA-256 dedup, CSV metadata),
  visual style editor and a settings tab (general, upload, style, contract,
  activity). Snapshots are cached between tabs and cleared on logout.
- `/admin/cola` now opens the campaign's lyric stage ("Revisión de letras").

### Fixed

- Preserve the legal `℗` mark when a selected font lacks the glyph, and keep
  compatible frame rates on the fast ProRes conversion path.

- Applying a change request whose quote is only part of the line no longer
  overwrites the whole line; only the quoted span is replaced ("…se fundió,
  dormite ya", "Que hace un año atrás").

### Changed (adversarial review, 2026-09-30)

- Review panel redesigned for zero-mouse review: one card at a time with a
  word diff (added in green, removed struck through, punctuation-only
  changes marked per sign), Enter/⌫/1–3/E/J/K/M/Z/? keys that never leak to
  the editor's own shortcuts, optional auto-listen, the lyric line of the
  active point highlighted (and flashed green after applying), undo that
  brings the point back, a failed fix that stays visible instead of
  vanishing, and an "Aprobar" button that turns into "Faltan N · Revisar".
- Visual pass: the panel follows the Genly design system (neutral surfaces,
  violet only for the primary action, green/red only for what enters and
  leaves the line, one progress bar); shortcuts, compared sources and the
  UMG guide live behind "?". Official lyrics enter through a single dialog
  shared with "Pegar letra oficial", with two explicit outcomes: Comparar
  (nothing changes) or Reemplazar y re-sincronizar.
- Fixes are applied by line identity and word position: correcting one
  repetition in a line no longer changes the other, a stale point never
  lands on a neighbouring line, merges keep word timings, chorus copies
  keep their punctuation.
- Precision: official-lyrics veto on sound-alike ears, phonetic
  corroboration, spelling-only differences only with the official lyrics,
  real questions and legitimate accent pairs no longer flagged,
  question/orphan points keyed by line, stable dismissal keys, NFC input.
  New suggestions: line breaks like the official lyrics and timing (a line
  that leaves before its last sung word).
- Safety and performance: bounded alignment (a 400-line repetitive song
  went from 45 s to under 1 s), review computed outside row locks and off
  the event loop and cached by content, lrclib fetched without holding a
  DB connection (max 2 at a time, own cache namespace) and never for batch
  campaigns (audio-only rule), correction memory scoped to the tenant,
  review reads in a savepoint, official-lyrics paste recorded as a product
  event.
- Change requests: a partial client quote replaces only its span, but a
  quote that only drops words (or matches the whole line) still replaces
  the line; leading ¿¡ and trailing punctuation are kept.

### Configuration

- `LYRIC_REVIEW_MODE`: `enforce` (default), `observe` (show without
  blocking) or `off`.
- `LYRIC_REVIEW_ENFORCE_TENANTS`: comma-separated tenants where approval is
  blocked; when unset, only batch campaigns block and everything else shows
  the panel without blocking.
- `LYRIC_REVIEW_FETCH_OFFICIAL`: `1` (default) fetches missing official
  lyrics from lrclib in the background; `0` disables it.

## [1.1.75] - 2026-09-30

### Fixed

- Return the live UMG approval gate from the job status endpoint, so an
  authorized campaign-scoped staging bypass is reflected in the review UI.
- Label the temporary preflight bypass clearly and hide stale-cut findings
  while preserving the independent lyric, file, and ProRes gates.

## [1.1.74] - 2026-09-30

### Changed

- Add a temporary, staging-only UMG preflight bypass requiring the existing
  exact-campaign allowlist and UTC expiry. The bypass is separately switched,
  audited at approval and publication, and leaves independent language,
  required-file and ProRes checks in place.

## [1.1.73] - 2026-09-29

### Added

- Preserve the private transcription snapshot before any automatic lyric or
  timing repair and let an operator undo the repair while the song remains
  untouched. The undo is versioned, reanalyzed, and excluded from human
  correction training data. The editor refreshes quality after undo instead
  of showing windows from the replaced version, and records undo usage.

### Safety

- Automatic repair still requires its existing signed acoustic calibration
  authorization and remains disabled until that evidence is available.

## [1.1.72] - 2026-09-29

### Changed

- Add a temporary, campaign-allowlisted staging exception for generic UMG
  REVIEW/NOT_RUN signoffs. Fresh QC, objective blocking failures, language
  review, render assets and ProRes freshness remain enforced; each bypass is
  recorded in approval and delivery audit events.

## [1.1.71] - 2026-09-29

### Fixed

- Refresh UMG delivery QC automatically when ProRes configuration changes its
  input fingerprint, then continue publication when the same reviewed render
  still passes. A changed or failing render returns the operator to the exact
  check that needs attention.

## [1.1.70] - 2026-09-28

### Fixed

- Keep the UMG publish confirmation visible when a follow-up request-list
  refresh fails, show live publish progress, and allow operators to reconcile
  an uncertain response against the exact change request before retrying.

## [1.1.69] - 2026-09-28

### Fixed

- Treat aliases of one recognition provider as one evidence family for lyric
  auto-repair. Optional candidate-provider errors now abstain without changing
  the original transcription verdict.

### Added

- Add a private, read-only Agus calibration queue with audio/checkpoint
  verification, blind clip review, controls and separate diagnostic handling
  for inserted or deleted lines. Operator edits remain hints, never gold labels
  or permission for automatic changes.

## [1.1.68] - 2026-09-28

### Fixed

- Reject LRCLIB results and cached lyrics whose title does not match the
  requested song, including the incident where "Hoy" selected "Hoy Es Adios".
- Require independent audio evidence before external lyrics can replace
  transcription, including the legacy fallback and gap-driven re-fetch paths.
- Keep live recordings on audio-owned structure when catalogue attestation is
  local-only, regardless of live policy switch settings.
- Log the selected catalogue record and audio/catalogue durations for future
  incident diagnosis.

## [1.1.67] - 2026-09-28

### Added

- Add a pre-editor lyric and timing auto-repair path with local-only candidate
  contracts, per-action signed calibration authorization, stale audio/segment
  guards, and a concise editor summary. Automatic changes remain disabled
  until an independently calibrated authorization is configured.

## [1.1.66] - 2026-09-28

### Fixed
- Preserve job state in UMG preflight refreshes so a completed analysis can satisfy the current-render readiness gate.
- Keep the refresh action available whenever the UMG gate requires a fresh preflight.

## [1.1.65] - 2026-09-28

### Fixed
- Keep UMG publication unavailable until the current render has a valid preflight, and send the operator directly to the required review.

## [1.1.64] - 2026-09-27

### Fixed
- Replace eight individual UMG checklist signatures with one audited review of the exact current render; explain publication blockers and distinguish unverified OCR from a pass.

## [1.1.63] - 2026-09-27

### Fixed
- Route UMG publication QC blocks to the current video's review checklist and return to the same change request afterward.

## [1.1.62] - 2026-09-18

### Fixed
- Corrected background variants replace the unique ancestor delivery with an open request in the chosen portal, retaining the delivery/request identity and archiving duplicate portal rows without deleting videos or files.
- Campaign publication releases transactions during large media copies and rechecks approval/publication identity before committing, matching the individual publisher; publication revisions increment only once.
- Clarify variant replacement in the publication picker and retain the current portal as the default destination when updating.

## [1.1.61] - 2026-09-18

### Fixed

- Release database transactions while copying large portal files, then
  revalidate the approved cut and destination before publishing atomically.
- Preserve legacy portal files without holding idle database locks, and
  never replace a publication completed concurrently by another operator.

## [1.1.60] - 2026-09-18

### Fixed

- Unify UMG correction review, exact-revision render approval and explicit
  publication from the editor and Changes. Expose manual resolution clearly.
- Pin published portal files independently from new renders, with an additive
  deliveries schema update and fail-closed publication copies.
- Reconcile campaign publication progress, refresh history after completion,
  and resolve older requests when a corrected cut is published.
- Allow platform administrators to regenerate approved backgrounds from the
  editor, retaining scene, permission and publication safeguards.

## [1.1.59] - 2026-09-17

### Fixed

- Verify saved UMG corrections across regenerated editor-local line IDs by
  falling back to unique unchanged timing. Do not match another chorus by text
  alone or accept ambiguous line timings as proof of an applied correction.

## [1.1.58] - 2026-09-17

### Fixed

- Isolate the remaining processing-job reaper contract test from the advisory
  lock held by lifespan daemons in the full suite; retain row-state assertions.
- Keep request analysis accessible beside the original comment even when a
  professional master is pending; do not mark unanalysed request steps complete.
- Keep UMG preview playback running when polling renews signed media URLs;
  reload only for a new object/render and expose an explicit media retry.
- Prepare configured ProRes masters with their saved format without invoking
  publication or requiring a pending-review render to be approved first.
  Show persistent acknowledgement and errors beside the action button.
- Keep saved-proposal details available and compare them against the current
  server lyric. Distinguish saved text from rendered video, and allow a fresh
  analysis when the saved text no longer matches the requested correction.

## [1.1.57] - 2026-09-17

### Fixed

- Recover applied UMG proposals from existing request-only editor links, so
  saved corrections can be approved without changing their text again.
  Validate the request, job and saved revision before enabling that render.
- Open the latest saved lyric correction instead of an older approved version.
  Reloading and approving now preserves the corrected phrase in the render
  payload; browser regressions cover both legacy and durable editors.

## [1.1.56] - 2026-09-17

### Fixed

- Preserve the exact applied UMG proposal when opening the lyric editor after
  reloading the change-request queue, so reviewing an already-saved correction
  triggers the intended re-render instead of reporting "No cambiaste nada".
- Let legacy UMG deliveries configure and regenerate their professional ProRes
  master while the completed video is in `pending_review`, while continuing to
  reject jobs that are still rendering.

## [1.1.55] - 2026-09-17

### Added

- Turn Cambios UMG into a dedicated full-width review workspace with a
  persistent request queue, video and timestamp context, editable diff,
  autosave status, workflow progress and a fixed primary action.

### Fixed

- Render a UMG lyric proposal that was already saved before opening the
  editor, even when there is no additional browser-side diff.
- Validate the exact request and applied proposal used to enqueue a render,
  make retries idempotent, and preserve that provenance in the render outbox.
- Return operators to the exact UMG request after submitting or completing a
  render instead of reopening the lyric editor without context.

## [1.1.54] - 2026-09-16

### Added

- Let operators regenerate visual backgrounds directly from a UMG change
  request, with an editable prompt and explicit preview before starting the
  existing re-render flow.
- Let legacy UMG deliveries restore their missing ProRes configuration from
  the change-request card, then prepare the professional master and refresh
  publication status automatically.

### Changed

- Parse multi-line UMG instructions as one reviewable request and preserve
  continuation text instead of dropping it after the first timestamp.
- Make delivery preflight findings concise and non-blocking unless an actual
  enforced approval gate fails.

### Fixed

- Always open post-render lyric edits from the latest approved revision, even
  when a newer unapproved autosave exists on the server.
- Archive incompatible browser drafts without blocking the editor behind a
  technical recovery dialog.
- Enable publishing corrected legacy deliveries after a real re-render; the
  new publication advances the revision, clears the old client approval and
  resolves the associated request. Failed renders remain blocked.

## [1.1.53] - 2026-09-16

### Added

- Turn UMG requests for a complete phrase on one screen into reviewable
  structural proposals when consecutive lyric fragments exactly match the
  client-supplied phrase and timestamps.
- Add a direct background-editor action to visual change requests, keeping
  image regeneration behind an explicit operator preview.

### Security

- Require human selection for structural merges, preserve the phrase's outer
  timing boundaries, and leave ambiguous or non-exact matches manual.
- Recalculate pending proposals created by an older parser without rewriting
  already applied proposal history.

## [1.1.52] - 2026-09-16

### Changed

- Promote UMG change requests to a dedicated, full-width Admin section with a
  visible pending-count badge, so operators can review the original request,
  current lyric and proposed result without competing with the live pipeline.
- Keep the live pipeline focused in the `Ahora` section and isolate editorial
  request data from the health and jobs polling lifecycle.

## [1.1.51] - 2026-09-16

### Added

- Show each change request beside the complete current lyric and a live preview
  of the resulting lyric before applying it. Selected operations and edited
  replacement text update the preview immediately, with changed lines shown
  against their originals and timestamps for context.

### Changed

- Require proposal edits to be saved before application so the reviewed
  preview is exactly the version that will be written.

### Security

- Mark a proposal stale in the read response when its bound lyric revision or
  content hash no longer matches the editor document, preventing an outdated
  contextual preview from being applied.

## [1.1.50] - 2026-09-16

### Added

- Add a revision- and audio-bound assistant for Universal delivery change
  requests. It understands the portal's real `timestamp + corrected lyric`
  lists, explicit replacements, repeated occurrences and terminal-period
  cleanup; operators can review, edit and selectively apply the diff from
  Operación without requiring official lyrics.
- Persist proposal lifecycle, decisions and idempotency in
  `change_request_proposals`, keeping ambiguous timing, structure, background
  and audio instructions review-only. Applying a proposal does not resolve the
  customer request: the existing re-render and publish step remains the only
  automatic closure point.
- Surface reference-free Delivery QC review signals for fragmented lyric
  cards, inconsistent near-repetitions and cards ending before their final
  stored word timestamp.

### Security

- Bind every proposal to the external request hash, editor revision and
  segment/audio identities; reject stale or conflicting writes and keep raw
  lyric text out of product analytics. Both analysis and application remain
  independently kill-switchable.

## [1.1.49] - 2026-09-16

### Fixed

- Refuse an on-demand ProRes preparation when the enterprise queue already
  has work waiting. That endpoint enqueues a multi-GB ffmpeg onto the same
  queue that serves client renders, and does it with the queue-depth guard
  deliberately bypassed — right for one human click waiting on a file, wrong
  for an avalanche. Measured on 2026-09-16 there are 178 missing files across
  both portals, so 178 buttons one click away, with production's rate limiter
  not throttling anything. Falls open when the queue cannot be read: a
  monitoring problem must not stop a client from asking for their file.

## [1.1.48] - 2026-09-16

### Fixed

- Bring the three portal commits production has been running into staging:
  on-demand ProRes preparation from the portal, its cross-environment variant
  for deliveries published from staging into the shared production portal,
  and the per-portal listing cache separation. Staging was a regression
  against production — promoting it would have removed features the client
  uses today, and the portal's "descargar" button on a missing ProRes would
  have started answering 404.

## [1.1.47] - 2026-09-16

### Fixed

- Build the delivery fingerprint from evidence that a render finished, not
  from its inputs. Touching `segments_revision`, `render_params` or
  `umg_spec` without re-rendering moved it, and publishing then cleared the
  client's approval and auto-closed their open change requests over a video
  nobody touched — reproduced on 29 rows. Conversely `/retry` and
  `edit_art_track` re-render from scratch and touch none of those, so a whole
  correction was reported as "the same cut". `completed_at` separates them.
- Catch a `/retry` in the ProRes gate. Those paths never archive the previous
  deliverables, so the staleness rule saw nothing and the pre-retry broadcast
  master could be published beside the new MP4 — the 2026-08-03 incident
  through another door. The publish path and the admin panel now also prove a
  re-render by comparing the render's completion against the publication.
- Write the freshness columns when publishing in bulk. Every row published by
  a campaign was born with no fingerprint, so drift detection was dead on
  exactly the rows the campaign view lists, and a campaign re-publish never
  cleared the "applying changes" flag.
- Drop `force=True` from the bulk ProRes prewarm. It deliberately bypasses the
  queue-depth guard, and this path can publish up to 500 songs at once — a
  thousand multi-GB transcodes ahead of every client render on the same queue.
  The portal's on-demand button, which is a human click, keeps it.

### Improved

- Tell a ProRes that is generated on demand from a file nothing will create.
  The daily audit was about to report 28 "phantoms" for a state the product
  chose on purpose, and an alert that cries without reason stops being read.
- Stop the audit from reporting a green day when it failed entirely or
  checked zero objects, always write its metrics so "clean" is
  distinguishable from "never ran", order the rows it truncates, and stop
  re-running the whole ~1000-object pass on every API deploy.

## [1.1.46] - 2026-09-16

### Fixed

- Identify the portal scope in `GET /api/deliveries/items`. The Chile portal
  fails closed when the listing does not say which portal it is for — by
  design, so umgchile.genly.pro can never render a stale backend's global
  listing — and production already returned the field while staging did not.
  Promoting staging would therefore have shown the client zero deliveries and
  an error. Verified live before and after.

## [1.1.45] - 2026-09-15

### Fixed

- Give the operator a timing instead of "No se pudo re-sincronizar" when every
  aligner declines a legitimately hard recording. Staging job `18dc85ecd8d6`
  ("Navidad de Aimogasta", 2026-09-15): the pasted 40-line official lyric was
  rejected by local CTC (median word score 0.29 < 0.30), by the hosted
  aligner, and by Whisper-DP (23 of 40 lines guessed by interpolation), so
  the operator got a generic failure and neither the text nor the timing was
  saved. Replaying the declined CTC candidate against an independent aligner
  confirmed the 0.30 floor was right — 23 of its 40 lines were off by more
  than 0.5 s and one run landed 13-18 s late — so the cascade gained a fourth,
  genuinely independent stage instead of a looser threshold: local Whisper
  **forced** alignment (stable-ts), which constrains the decoder to the
  operator's text, runs on CPU in ~5 s, costs nothing per call and
  interpolates no line. On that job it timed all 40 lines with zero crammed
  lines and flagged the 10 least confident for review. It runs only after the
  three existing engines have declined, and every shared guard still applies:
  the same `_safe_alignment` verdict, the same crammed-line guard, the same
  fail-closed ending. Two negative controls over the same audio still decline
  — another song's lyric (Color Esperanza) and the same lyric duplicated to
  80 lines. Kill switch: `ANCHOR_LOCAL_ALIGN_ENABLED=0`.

## [1.1.45] - 2026-09-15

### Added

- Audit what the portal promises, once a day, in the reaper's single-runner
  cycle. Two classes of failure were invisible until someone asked about
  something else: a row advertising a file that is not in R2 (28 of 34 Chile
  deliveries on 2026-09-15), and a delivery whose render changed after it was
  published. It only reports — repairing a deliverable is always a decision
  with a human in it, because the row can lie in both directions. Rows with
  no fingerprint and jobs owned by another environment are counted as
  unevaluable rather than flagged, and an R2 network failure is never
  reported as a missing file.

### Improved

- Show the published version, the in-flight state and "atendido en la versión
  N" on the client portal, and make a missing ProRes read as the download it
  is — clicking it generates the file and then starts the download, instead of
  asking for a second click three minutes later.

## [1.1.44] - 2026-09-15

### Fixed

- Stop the bulk campaign publish from promising a ProRes that nothing will
  create. It listed both `.mov` deliverables in `file_types` without checking
  them, on the assumption that the portal materialises ProRes on first
  download — it does not: the portal signs the deterministic R2 key and never
  goes through `ensure_prores_exists`. Measured on the Chile portal on
  2026-09-15: 28 of 34 active deliveries were offering UMG a "ProRes Master
  (broadcast)" that does not exist in R2. A job without `umg_spec` cannot
  produce one, so it is now published as a partial delivery instead; a job
  with a spec gets the transcode queued so the file actually appears.

## [1.1.43] - 2026-09-15

### Fixed

- Stop renaming a delivery when it is re-published. The default label counts
  the active deliveries for that song and the row being updated counted
  itself, so shipping a correction rebaptised "Campaña" as "Opción 2" — a
  second option that does not exist — on the client's screen. An explicit
  label still wins, and a genuinely new delivery of the same song still gets
  "Opción N".
- Read the ProRes obligation from the delivery's own `file_types` instead of
  the job's profile columns. A job can carry `delivery_profile="youtube"` and
  `umg_spec` as JSON `null` — which is not SQL NULL, so `umg_spec IS NOT NULL`
  returns true and misleads every diagnostic query — and still have a
  published broadcast master. In that shape the freshness check found nothing
  and the portal kept serving a pre-edit master.
- Say what is actually wrong when a stale master has no ProRes spec, instead
  of "this video was generated for YouTube only" about a video the client
  already received as a delivery.

## [1.1.42] - 2026-09-15

### Fixed

- Refuse to publish a delivery while its broadcast master is still the
  pre-edit cut. The portal signs the deterministic R2 key, where an edit
  leaves the old `.mov` answering a HEAD until the asynchronous re-transcode
  overwrites it, so the existence check passed and Universal could download
  a stale ProRes master beside the corrected MP4. The gate now reads the
  job's tracked keys and force-queues the missing master instead.
- Take a portal approval down when new content is published over it. The
  client kept seeing their own green "approved" pill, and no Approve button,
  on a version they had never reviewed.
- Invalidate the portal's cached file size when the published content
  changes. It has a 30-day TTL, so a re-render advertised the previous
  file's weight — the one clue the client had that anything had changed.

### Improved

- Track publication revision, render fingerprint and in-flight state per
  delivery, and expose them to the portal: a re-send of the same cut is now
  distinguishable from a new version, and a re-render in progress no longer
  presents the download as final.
- Close a change request by publishing the correction that answers it,
  recording which revision did so. Resolving by hand stays available for
  what is answered without a re-render, and is labelled as such.
- Show each change request's real publication state in the admin, with
  "editar letra" and "publicar actualización" on the card. The three steps
  of a correction used to be spread across three screens with nothing
  connecting them.
- Flag campaign videos whose portal publication is outdated, with a filter
  for them: a corrected video that was never re-published looked identical
  to one that was.

## [1.1.41] - 2026-09-15

### Improved

- Show actual Argentina/Chile portal publication badges and pending customer
  change-request counts in campaign video history, including videos being edited.
- Filter sent and unsent videos alongside search and approval status; preserve
  the filter when opening the existing lyrics editor and returning to the campaign.
- Read active publications from the portal database, excluding removed deliveries
  and unrelated tenants instead of treating approval as proof of delivery.

- Let platform administrators read the delivery operations they created for
  another campaign tenant, preserving ordinary tenant isolation.

## [1.1.40] - 2026-09-15

### Improved

- Make the campaign workflow explicit: lyrics, video generation, video review,
  and deliverables, preserving search and navigation context between steps.
- Search campaign songs by title, artist, code, or filename regardless of
  accents and word order; keep bulk actions within the filtered selection.
- Approve and advance inside the campaign player, and track portal deliveries
  with visible errors and stable retry identifiers.
- Paginate large review queues, adapt rows and dialogs to mobile, and correct
  art-track delivery previews and their result messages.

## [1.1.39] - 2026-09-15

### Fixed

- Explain pasted-lyric differences as a comparison with editor text, without
  presenting transcript overlap as proof of accuracy against the recording.
- Accept identical normalized lyrics with different line breaks without an
  unnecessary structure confirmation; retain guards for unmatched passages.
- Show a blocking progress dialog throughout lyric re-synchronization and
  delayed-response recovery, restoring editor interaction on completion or error.

## [1.1.38] - 2026-09-15

### Added

- Add 1×, 1.5× and 2× playback speeds to the lyrics editor audio toolbar,
  preserving pitch and lyric timestamps while reviewing the advanced timeline.
- Keep the selected speed across pauses and audio-source renewals, with
  keyboard-accessible controls in Spanish, English and Portuguese.

## [1.1.37] - 2026-09-15

### Fixed

- Require material saves paired with active-editor activity before identifying
  saved human changes; automatic saves no longer inflate that filter.
- Show saved human changes inside pending songs and explain the campaign total
  as pending plus approved plus discarded, preserving existing review links.

## [1.1.36] - 2026-09-15

### Fixed

- Enforce Veo Lite for every new video background, including static shots,
  previews, scenes and edits; remove Fast and environment model overrides.
- Apply the Lite policy to older campaign assignments for future generation,
  preserving the original requested model in new receipts and historical audit.
- Bind preview caches and model disclosures to Lite; restrict generation tools
  to the same policy and remove the campaign model selector.

## [1.1.32] - 2026-09-15

### Fixed

- Recover missing campaign audio references only from the validated immutable
  full-audio machine snapshot bound to the same recording and revision, so
  legacy quality replays cannot leave an otherwise reviewed song blocked.
- Show the specific audio-reference rejection during campaign approval and
  reserve automatic revision retries for real editor conflicts.

## [1.1.31] - 2026-09-15

### Fixed

- Explain when campaign generation reaches the render or final-review capacity
  limit, stop further submissions, and keep unsent songs selected for retry.
- Show each failed song's rejection reason and preserve submission results
  when refreshing campaign status fails.

## [1.1.30] - 2026-09-15

### Fixed

- Canonicalize re-anchored timestamps before persisting and scheduling quality
  analysis, so real aligner precision cannot leave quality checks waiting on
  a snapshot hash that differs from the editor's saved revision.

## [1.1.29] - 2026-09-15

### Fixed

- Bind quality checks to the new re-anchored revision and atomically queue
  their reanalysis, preserving recovery if queue publication is delayed.
- Refresh quality warnings after re-anchoring or pasting lyrics, including
  recovered requests, without overwriting edits made while checks run.
- Avoid presenting per-line alignment flags as the total quality review count
  in successful re-anchor and paste notifications.

## [1.1.28] - 2026-09-14

### Fixed

- Preserve list and mapping evidence from delivery detectors when building
  final-render checks, so real findings no longer prevent fresh QC reports
  from being saved after rendering or editing a video.

## [1.1.27] - 2026-09-14

### Fixed

- Show each delivery preflight check as `PASS`, `FAIL`, `REVIEW` or `NOT_RUN`.
- Keep generic reviewer reminders visible without treating them as failed
  videos, and block approval only for objective detector failures with
  evidence.
- Make technical and metadata mismatches deterministic blocking failures and
  expose accurate failure/review counters to the operator.

## [1.1.26] - 2026-09-14

### Fixed

- Allow operators to refresh stale Delivery QC reports from existing rendered
  campaign videos before approval, and show structured approval errors as clear
  actionable messages instead of `[object Object]`.
- Accept the standard artist-plus-title title card in metadata and OCR checks
  without weakening detection of title suffix mismatches.

## [1.1.25] - 2026-09-13

### Fixed

- Add opt-in prospective capture eligibility for first campaign transcriptions,
  excluding technical smokes, previous outputs and existing editor documents
  before reserving evidence slots. Preserve bounded private checkpoints across
  retries independently of human document persistence. The association repair
  remains disabled pending exact baseline replay and full-document evaluation.

## [1.1.24] - 2026-09-12

### Fixed

- Resolve adjacent connector cascades at a caption boundary in one bounded,
  idempotent pass, so phrases such as `y en / Mi corazón` and
  `a mi / Alrededor` cannot require a second processing run. Segment count,
  lexical content, word order, and every approved start/end value remain exact.

## [1.1.23] - 2026-09-11

### Fixed

- Keep short Spanish connectors and articles with the phrase that follows when
  word timestamps prove they sit at the next caption boundary, without changing
  segment count or any approved start/end value.
- Stop adding prose-style full stops to lyric caption endings and remove a
  single terminal period deterministically while preserving questions,
  exclamations, ellipses, vocabulary, word order, and timing.

## [1.1.22] - 2026-09-11

### Changed

- Replace oversized campaign video cards with a searchable, paginated compact
  list. Load one medium-sized player on demand and expose approval, editing,
  detail, and delivery actions from each row.

### Fixed

- Show immediate, live progress while a campaign submits multiple video jobs,
  including the current song, submitted count, and an explicit duplicate-click
  warning instead of leaving a disabled confirmation modal that appears inert.
- Renew the exact lyric-and-timing attestation before re-rendering a campaign
  correction, preserve the existing final-review state between requests, and
  let that re-render reuse its own bounded review slot.

## [1.1.21] - 2026-09-11

### Changed

- Simplify the campaign style/background workspace around the daily workflow:
  show status totals, filter ready/pending/generating/approved songs, select only
  approved lyrics in one action, and keep the generation action above the table.
- Collapse contractual style distribution and advanced visual controls until an
  operator explicitly opens them, while retaining the complete audited workflow.

### Fixed

- Exclude discarded songs from the ready-to-generate bulk selection and display
  the number of selected songs that are actually eligible before confirmation.

## [1.1.20] - 2026-09-11

### Added

- Build lyric-inspired backgrounds from a lyric-only semantic pass whose visual
  anchors must cite the supplied song, then compose a detailed 240–320 word
  provider prompt around those verified anchors.
- Check generated background frames for readable text, logos, black bars,
  unstable lighting and scene changes before accepting them for lyric videos.

### Fixed

- Keep the API and rendering workers in one lyric-anchor rollout mode, reject
  invented or partial-word anchor evidence, and retain a safe legacy fallback
  when lyric analysis is unavailable.

## [1.1.19] - 2026-09-11

### Fixed

- Separate local draft recovery from server save errors. Compare browser copies
  before removing them, require an explicit recovery/discard choice for different
  content, and preserve unreadable copies without changing server approval.
  Copies that would change during normalization are retained, including zero or
  sub-minimum durations that would otherwise falsely match the server.

## [1.1.18] - 2026-09-11

### Added

- Opt-in, expiring staging capture for six ordinary transcriptions: processed
  reconciliation inputs, route/audio/code/config identity, and subsequent
  cleanup and presentation evidence in the existing private machine snapshot.
  Includes an offline baseline-first replay tool. The word-association behavior
  candidate remains disabled; no extra transcription is scheduled.

## [1.1.17] - 2026-09-11

### Fixed

- Open approved campaign lyrics directly in the transcription editor, even before a video exists; preserve the approved-list return context and keep final-video navigation separate.
- Return an approved, unrendered campaign song to human review when its transcript actually changes. Keep unchanged saves approved, retain the previous approval in the audit/history, and allow approval of the corrected revision.

## [1.1.16] - 2026-09-11

### Fixed

- Let campaign reviewers split a lyric line at the text caret with Enter. When
  word-level timing is available, both new lines retain the original word
  timestamps so reviewers can regroup poorly segmented phrases without
  retiming the song.

## [1.1.15] - 2026-09-10

### Fixed

- Print the complete campaign contract report on white pages without clipping it inside the application layout; preserve normal printing on other screens.

## [1.1.14] - 2026-09-10

### Added

- Campaign creative workspace: select songs across pages and assign complete styles or individual visual fields by percentage/count. Preview a stable allocation, preserve pinned exceptions, record rounding and undo the latest safe assignment.
- Versioned contractual groups for fixed photo plus effect or an explicit Veo Fast/Lite model selected per group, with per-song settings, actor/date/reason, render fingerprints, applied-effect evidence, delivery records and CSV/Excel/print-to-PDF reports.
- Campaign video history includes native generations and related variants. Generation uses the existing human lyric/timing approval and queue protections; saving styles never generates media.

### Fixed

- Keep uploaded photos still when selecting Foto fija, both on the first render and on edits; effects remain independently composited.
- Restore all campaign visual settings in the individual editor, autosave changes with revision checks and flush before approval or switching songs.
- Preserve campaign membership in new variants and keep rendered evidence separate from requested settings.
- Recognize published aliases of already reviewed Python advisories without extending security exceptions or their expiry.

## [1.1.13] - 2026-09-10

### Fixed

- Preserve spreadsheet lyric decisions and published reviewer candidate receipts when background quality analysis finishes. Reviewers retain source outcomes and candidate pointers; existing source checks still reject stale candidates.

## [1.1.12] - 2026-09-10

### Fixed

- Allow operator text corrections that preserve every timestamp when another
  existing line overlaps. Keep source, ownership, structural and approval checks;
  do not silently repair timing or permit a proposal to introduce overlaps.
- Discard stale word timestamps when reviewer suggestions replace text, matching
  the isolated candidate and preserving the need to validate its new word timing.
## [1.1.11] - 2026-09-10

### Added

- Import audio-bound spreadsheet lyrics with batch campaigns. The worker compares each candidate against audio-first recognition before alignment, preserves independent acoustic evidence, and exposes applied, declined, and absent sources to human reviewers. Existing campaigns retain their audio-only behavior.

### Fixed

- Keep whole-song reference alignment disabled when an unsupported passage is hidden by otherwise matching choruses. Local vocabulary reconciliation remains available.

## [1.1.10] - 2026-09-09

### Added

- Add a Borradores campaign tab with saved edits, recent-first ordering, author and save time, searchable songs and a direct resume action. Existing drafts are discoverable; approved and discarded songs are excluded without deleting their edits.

## [1.1.9] - 2026-09-10

### Fixed

- Keep complete audio-derived hypotheses available for discrepancy diagnostics
  and conservative post-pass language resolution when global alignment is rejected.
  Rejection still prohibits copying reference lyrics into the transcript.
- Detect adjacent unexplained repetitive phrases without counting only distinct
  words or diluting a local failure across the whole song. Preserve vocal-only
  adlibs, bilingual references, editorial documents and existing approval resolution.
  These checks detect drift; they do not claim to repair the ASR's wrong words.
- Resolve a pending complete-audio reference before Auto studio recognition so
  the initial language hint is no longer skipped by the parallel batch path.
  Veto the hint for a short foreign verse; never send derived lyrics as a prompt.
  Explicit choices and live Auto keep their existing scheduling. No extra calls.
- Make discrepancy resolution reachable from the approval action and fixed footer.
  Save first, attest the exact saved revision, surface failures and keep the server
  approval gate. Do not approve or alter songs on behalf of reviewers.
- Restrict reference suggestions to unambiguous near-orthographic changes, bind
  them to current stable line IDs and stop proposing unrelated or stale verses.

## [1.1.8] - 2026-09-09

### Fixed

- Keep campaign review URL, editor and audio bound to the selected song when navigating back or switching songs; preserve the campaign return link.
- Load a campaign queue in one request, defer administrative item loading and debounce search.
- Explain review actions and evidence in plain Spanish; expose uncalibrated confidence and remove duplicated navigation.

### Added

- Recoverable campaign discard with reason, actor, timestamp, original status and append-only audit events. Discarded songs stay out of pending review and retain audio and drafts.


## [1.1.6] - 2026-09-09

### Fixed

- Preserve forced-alignment provenance through repeated evidence annotation and
  retain native provider probability with its original meaning, not as CTC score.
- Diagnose internal word gaps with unusable timing support. Display timing as
  unvalidated in the editor without replacing text, retiming or changing approval.
  This release demonstrates preservation and detection, not timing repair.
- Includes the already-served #1306 language/reference discrepancy warning,
  persisted review resolution and server approval gates, and Sentry 2.69.1 pin.
  Version 1.1.5 was reserved for that fix but was not separately released.
- Isolate Playwright's Ubuntu dependency setup from the unrelated Google Chrome
  APT feed, whose inconsistent package index blocked CI; retain all browser gates.

## [1.1.7] - 2026-09-09

### Added

- Add parallel Art Track campaigns for up to 500 audio+cover pairs, with resumable uploads, deterministic/manual association, mandatory human review and durable bulk delivery operations.
- Persist delivery destination with `Delivery.portal_id` and isolate Argentina/Chile portal listings, tokens and publish operations.

## [1.1.4] - 2026-09-09

### Fixed

- Make reviewer cards and pending/approved/all tabs actionable, keep campaign
  counts stable, and restore filters with browser history and editor return.
- Prevent late requests and conflicting status filters from hiding approved
  songs. Show the first queue page promptly and avoid overlapping refreshes.
- Clarify processing, approval, export and reviewer-lock states. Show actionable
  durable-editor load errors with readable retry, job ID and HTTP status while
  keeping editing and approval blocked until the saved revision is loaded.

## [1.1.3] - 2026-09-06

### Fixed

- Approval validates real overlaps without silently imposing a 50 ms gap or
  modifying saved endpoints, including protected lines. Remove the remaining
  mount-time text-length trim outside campaign mode; trimming is explicit only.
- Reconstruct line ownership from contiguous editor history and exact replay
  of audited hold/punctuation migrations. Unknown provenance, human edits,
  locks and approved songs remain protected; publication verifies live history.
- Persist campaign correction-capture intent atomically with approval through
  the existing outbox and idempotent learning queue. Operational observations
  retain version/audio/exposure evidence and distinguish structural changes,
  automatic migrations and endpoint candidates, without training or gold promotion.
- Ignore only four-decimal persistence-equivalent timing noise. Show auxiliary
  reference discrepancies in candidate doubts; preflight PASS is not certification.

## [1.1.2] - 2026-09-06

### Fixed

- Classify inline source URLs in lyric spreadsheets as pointers, never lyric
  text. Preserve raw cells and URLs without fetching linked content.
- Add cache-only Excel-guided phrase reconciliation with dual blind audio
  occurrence evidence, explicit abstentions and protected human revisions.
- Run the existing delivery preflight before and after isolated repair; add
  its high-confidence spelling suggestions through a distinct deterministic
  path, without inventing audio witnesses. Exclude valid "aventuras" and verb
  neighbors from the near-match typo rule.
- Permit immutable candidate generations for the same source through a
  source-checked campaign pointer, retaining legacy reads and prior evidence.
  Publishing a generation never changes an editor document or approval.

## [1.1.1] - 2026-09-06

### Fixed

- Immutable reviewer-candidate R2 writes now sign exactly one create-only
  header on both old and new SDKs. Duplicate signed values previously caused
  `SignatureDoesNotMatch` and blocked campaign publication in staging.
- Preserve conditional creation and source protections; no inference, timing
  automation, document replacement or approval is enabled by this hotfix.

## [1.1.0] - 2026-09-06

### Added

- Campaign-scoped, supervised full-song reviewer candidates in the existing
  campaign and editor workflow, with source-bound coverage, highlighted text
  changes, localized doubts and full audio playback. Publication never approves
  a song or replaces the current editor document.
- Resumable two-family acoustic review, conservative in-flight API accounting,
  bounded recovery and immutable candidate artifacts. Listening and display are
  independently gated; opening a campaign cannot buy another review.
- Opt-in capture of ordinary human timing corrections, preserving their source,
  author and revision. Historical development comparisons are not clean gold.

### Fixed

- Editor autosave now retains local text/timing changes made while an earlier
  save or conflict reconciliation is in flight.
- Local-only reconciliation reuses its exclusively owned evidence inventory.

### Release scope

- Staging only, explicitly allowlisted campaign; no production promotion.
- Automatic timing repair, automatic approval and paid runtime inference remain
  off. Acoustic coverage is not correctness certification or measured time saved.

## [1.0.1] - 2026-09-05

### Fixed

- Offline endpoint evaluation now derives dependency groups from songs,
  artists, related recordings, jobs and audio hashes instead of trusting
  per-line unit IDs. Exploratory confidence uses grouped evidence.
- Control damage is conditioned on actual changes, with conservative grouped
  rare-risk bounds. No-op changes and ambiguous applications cannot inflate
  evidence; different component names do not attest independent model families.
- No runtime timing mutation, training, or automatic promotion is enabled.

## [1.0.0] - 2026-05-22

**Public launch.** First customer: Universal Music Argentina (200 videos/month).
Everything below is the baseline shipping at launch.

### Added — Rotor-grade timing pipeline

- **Audio as the source of truth for timing.** Lyric sources (lrclib, Gemini,
  user-pasted) are treated as text hints; the actual uploaded audio decides where
  each line falls.
- **Forced alignment of known lyrics to the audio** (stable-ts/whisperX via
  Replicate, ±50ms). New `forced_align.py` with drift detection — falls back
  cleanly when the model's tokenisation diverges from the lyric text.
- **Vocal source separation (demucs)** before transcription/alignment. Variant
  `mdx_extra` from arXiv 2506.15514 (WER 47%→28% on lyric transcription).
- **WhisperX for the no-lyrics path** — word-level timestamps (<100ms) when
  lrclib has no entry. Returns per-word stamps that survive editing.
- **lrclib content verification.** Every synced timestamp set is verified by
  slicing 5s of audio at the claimed line start and fuzzy-matching against
  Whisper output. Falls back to plain+Whisper when confidence drops below 0.4.

### Added — Rendering & overlay capabilities

- **libass-based render path** for crisp subtitle compositing at HD/4K with
  consistent kerning across font weights.
- **FX overlay layer** (baked RGB loops, blend=screen, color grade) — adds the
  "overlay-effects" tier UMG reference videos rely on.
- **Karaoke / reveal / pop / glow letter animations** in the wizard.
- **ProRes 4444 + UMG master profile** for delivery to label QC pipelines.

### Added — Wizard / Studio Console

- Multi-step Studio Console wizard for the operator: audio upload → letra →
  efecto → fondo → revisión, with live preview at every step.
- "Eje efecto encima" gallery with side-by-side preview and per-line FX layering.
- Lateral / calm camera motion presets only by default (forward-camera-travel
  causes nausea when overlaid with lyrics — UMG bias policy).

### Added — Hallucination & quality guards

- "Sparse mega-segment" detector in `_detect_hallucination`
  (`dur>30 AND words/dur<0.1`) — catches the case where Whisper emits a single
  long segment with 3 words ("Música de presentación" on the song "El Arbol").
- Gemini-grounded lyric recovery when Whisper output is hallucinated and lrclib
  has no entry — distributes reference lines into the silent gaps using
  librosa VAD anchors.
- Audio compression before Replicate forced-align upload (handles `Broken pipe`
  on long files) with automatic retry.

### Added — SaaS infrastructure

- PostgreSQL-backed jobs/users/billing with concurrent worker pool, per-user
  rate limiting, and admin overrides.
- Stripe subscriptions + per-video billing with overage and concurrency caps.
- Docker + Railway deploy with separate staging and production environments,
  workspace-aware ship workflow, and reaper for stuck jobs.
- Email flows (verification, password reset, video-ready, payment receipts).
- Admin dashboard for operator-driven reviews and re-renders.

### Configuration

New environment flags, all default OFF except where noted:

| Flag | Default | Purpose |
| --- | --- | --- |
| `LRCLIB_VERIFY_ALWAYS` | `1` (ON) | Run lrclib content verification on every synced hit |
| `LRCLIB_VERIFY_THRESHOLD` | `0.4` | Min fuzzy-match confidence to trust lrclib |
| `FORCED_ALIGNER_ENABLED` | `0` | Enable forced alignment of known lyrics |
| `VOCAL_SEP_ENABLED` | `0` | Enable demucs vocal separation |
| `DEMUCS_VARIANT` | `mdx_extra` | Internal demucs checkpoint |
| `WHISPERX_ENABLED` | `0` | Enable whisperX for the no-lyrics path |
| `REPLICATE_API_TOKEN` | — | Required for any of the above Replicate engines |

[1.0.0]: https://github.com/tometh22/VideoLyricsIA/releases/tag/v1.0.0
