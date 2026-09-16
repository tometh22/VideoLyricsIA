# Changelog

All notable changes to VideoLyricsIA (GenLy AI) are documented here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
