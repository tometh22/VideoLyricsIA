# Corrections hardening: audited implementation contract

2026-09-18. Revised after independent architecture, adversarial parser, and UX/test audits. This is a staged implementation ledger, not a declaration of completion.

## Scope and release ownership

User requested audits first, then implementation and robust stress/regression tests, then staging deployment and staging E2E only after gates pass. Istanbul root owns integration, version triplet, migrations, merge order, deployment and verification. No production deployment, customer content mutation/publication or destructive retention policy expansion is authorized by this staging request.

The broader roadmap includes new integration contracts, publication history/receipts, operational/sandbox separation, timing assistance and constraint-driven backgrounds. A first safety slice is not the entire roadmap. Track incomplete or coordinated-production work explicitly; do not call DTO scaffolding, disabled flags or mocked E2E completion.

## Audit additions adopted

1. Source-preserving conservative interpretation: negation, conditional or ambiguous instructions never become affirmative lyric replacements. Every meaningful block has an instruction or explicit abstention; no truncation presented as comprehension.
2. Local scope by default; exact Unicode-aware text mutation, lexical boundaries, temporal ambiguity and merge-gap checks. Reject overlapping patches before presenting them as jointly applicable.
3. Preview binding includes proposal content hash, not only editor revision. Concurrent PATCH/APPLY must not apply text the confirming operator never saw.
4. Preserve all mandatory background constraints; trim optional context first. Prompt only includes visual request content, not unrelated lyric instructions.
5. UI never promotes QC FAIL to PASS or hides effective blockers. Distinguish measured failure, heuristic finding, required human review and unavailable verification.
6. All action notices belong to a case. Stale async responses cannot replace current context. Lost mutation response means unknown outcome/reconciliation, not confirmed failure.
7. Publication and resolution are separate facts. No implicit closing of every older request; explicit reviewed case/revision required. Manual close needs a reason; historical absence remains unknown, not fabricated.
8. Published snapshot integrity uses published keys and tri-state storage status. A newer draft is not a broken publication. Budget exhaustion is unchecked, not healthy.
9. Retention first gets a read-only inventory/dry-run and protective behavior; no new snapshot deletion. Production and staging reapers share deliveries but not necessarily locks. A staging fix cannot certify the old production sweeper.
10. No remote storage I/O under a DB transaction or on an async event loop. Preserve post-I/O CAS and existing approval/tenant gates.
11. Immutable files must also be a coherent package. Source mutation during copying must fail closed; multipart ETag is not a full-file SHA256 proof.
12. Portal approval/unapproval and feedback must eventually bind the publication the client viewed, with idempotency and revision fencing. Requires coordinated portal-reader/frontend deployment; staging-only work cannot claim this completed.

## Implementation slices and dependencies

| Slice | Owner | Files / responsibility | Status |
|---|---|---|---|
| S1 Interpretation and proposal binding | Parser implementer; root API integration | parser, proposals, pure/adversarial tests; root wires proposal hash into API | Implemented locally; final regression validation in progress |
| S2 Snapshot integrity and retention safety | Architecture implementer | integrity, retention, focused tests; no destructive policy expansion | Implemented locally; production reaper unchanged |
| S3 UX races, uncertainty and honest QC | UX implementer; root backend QC | hook/panel/QC components and tests; server remains authority | Implemented locally; final acknowledgement regressions in progress |
| S4 Shared state, explicit resolution and I/O fences | Root | workflow projection, publication/resolve APIs, cross-entry contract and tests | Partial: server projection, explicit-case closure, proposal/QC CAS and scoped I/O fixes; not a unified domain service |
| S5 Integration/fault/stress harness | Root + independent test review | isolated PostgreSQL, storage/queue adapters, real browser/media, replayable fault tests | Local gates passed: real PostgreSQL, fault injection, concurrent HTTP, 915-request/32-concurrent replay soak, real media; complete real correction-to-portal journey not verified |
| S6 Pre-release adversarial diff review | Independent auditors | all P0 findings disposition, red/green evidence and oracle review | Three independent reviewers; all identified P0/P1 findings fixed and targeted regressions green |
| S7 Clean CI then staging deployment/E2E | Root only | release triplet, protected CI, verified isolation, fleet/source checks | Not authorized until gates pass |
| S8 Publication-domain evolution and portal revision contracts | Coordinated future consumer work | immutable history/receipts, feedback-origin and client-approval revisions, migrations | Requires coordinated production scope; not solved by S7 |
| S9 Contextual automation/new connectors | Subsequent integrated slices | timing/layout previews, budgets, independent evaluation, destination adapters | Depends on verified domain contracts |

## Test gates

- G0: regressions for every reproduced parser/QC/UI defect; whole-result assertions, no partial-match green. Deterministic seeded adversarial corpus and input/time/memory bounds.
- G1: PostgreSQL integration, schema-realistic IDs, explicit transaction barriers, edits/patches/publications racing. Lost response after commit, storage failure/unknown, duplicate queue delivery, request arriving during publish. Existing document/audio/request/idempotency guards preserved.
- G2: real browser + real local API/database + synthetic rendered media/storage consumer. A published, B edited/rendered, portal still A; publish B then independent portal response/bytes/metadata B. Mocked browser tests reported separately.
- G3: isolated deterministic state sequences and bounded concurrency/load/soak. Initial targets: 10,000 state interleavings, parser fuzz >=1,000 seeded variants, concurrency levels 2/8/32 with fault barriers. Baseline latency/resources before setting budgets. No paid model inference or production stress.
- G4: independent final diff review, clean-checkout required CI, version triplet, no unresolved release-critical regressions; reader/writer compatibility and rollback tested when schema changes.
- G5: staging mutation preflight proves isolation of jobs/deliveries/storage/queues/billing/notifications/portal from customers. ENVIRONMENT=staging or a test name is insufficient. Missing isolation blocks mutating E2E, never a green skip. Actual customer smoke is read-only. Heavy stress stays local/disposable.

## Explicit no-go conditions

- Unsafe parser fallback, missing instruction coverage, omitted visual exclusion, local action affects unrelated content, stale proposal preview accepted.
- UI says zero blockers while server rejects without showing a reason; no-data becomes PASS/published/resolved.
- Concurrent edits are lost; duplicated operation charges/effects; unselected cases resolve; candidate work changes published bytes prematurely.
- Unknown storage/transport outcome treated as failure or success without reconciliation.
- Old shared-domain writer/reaper can violate a newly claimed invariant and compatibility has not been resolved.
- Full-journey gate is mocked/skipped but described as real E2E; synthetic fixtures leak into AR/CL customer portals.

Each result must record exact SHA, environment, test type, executed/skipped counts, seeds and remaining limits. State-machine tests do not by themselves prove production concurrency; check real implementation with barriers. A reviewed checkbox is an attestation, not proof of complete audiovisual listening.

## Baseline

Base: origin/staging 49166113 (1.1.62). New branch/worktree: tometh22/umg-system-hardening, within Istanbul .context. Root worktree preserved.

Isolated local PostgreSQL16 database genly_hardening_20260918; root sole test executor. Initial focused baseline: 38 tests PASS with Python3.11. Python3.9 collection failed because the application requires modern union-type syntax; not a product regression. The initial global environment had FastAPI0.135.1 rather than pinned0.141.1 and Stripe15 rather than required8. A separate local venv now uses FastAPI0.141.1, Starlette1.6.0 and Stripe8.11.0, with missing test imports installed and `pip check` passing. This venv inherits system packages; clean-checkout exact-dependency CI remains mandatory.

No customer or production changes have been performed for this work.

## Evidence boundaries and release blockers

- Local real database tests use PostgreSQL16; CI uses PostgreSQL18. Both are needed, not interchangeable certification.
- The 1,000 seeded parser variants are deterministic fuzz tests. The 10,000 state observations test a pure projection, **not** 10,000 concurrent transactional interleavings.
- Concurrent apply tests use 2/8/32 actual HTTP callers and PostgreSQL row locks. A separate bounded replay soak uses 25 waves of 32 callers and simulated lost responses. Neither measures production model/render throughput or establishes a p95 SLO.
- Snapshot tests emulate object storage and inject mutation/failure at copy boundaries. Conditional ETag copying prevents detected source changes but cannot establish that an already-mixed source package came from one render. A writer-owned immutable artifact manifest remains S8.
- No schema migration has been added. Publication history/receipts, consumer approval/feedback revision binding, shared-domain old writers/reapers, full unified command service, new destination adapters, and contextual timing automation are **not completed** by this slice.
- The installed local ffmpeg builds do not expose libass; Docker daemon is unavailable. The real MoviePy/libass render gate must run in a suitable environment and must not be counted as passed through skips.
- Staging currently shares deliveries/storage with live AR/CL portals. Isolated staging mutation E2E needs separate resources/approval; a staging label or synthetic song title does not prove isolation. The operator has been asked about this explicitly. No customer-mutating staging test is authorized by default.
- Any deployment requires the release version triplet, clean-checkout CI, final independent review disposition, applicable real-stack gates and one release owner. No deployment or customer publication has occurred in this task.

## Final local evidence (2026-09-18)

The implementation worktree is `tometh22/umg-system-hardening`, based on staging
`49166113`. The root ran all heavy jobs serially against disposable PostgreSQL
databases; no Universal rows, storage keys, queue, notification or billing
resources were used.

- Full backend after the publication and `/edit` lock fixes: 5,780 passed, 6
  render-dependency skips, 5 deselected. The only three initial failures were
  an existing background-preview test leaking a synthetic cache key across
  tests; explicit cache-miss isolation was added and its 8 cases pass.
- Final targeted correction/publication suite: 817 passed. It includes parser
  adversarial corpus, 2/8/32 apply races, background proposal fencing, QC
  report fencing, snapshot coherence, portal A→B consumer checks and concurrent
  publication/ProRes retries.
- Replay soak: 915 HTTP apply attempts in 25 waves at concurrency 32; one
  durable revision, no leaked connections, p95 request time 0.1954s in this
  local run. This is a bounded regression guard, not a production capacity SLO.
- Real media gate: 14 MoviePy/FFmpeg/libass tests passed using the local
  imageio FFmpeg binary. Frontend: 1,446 Vitest tests passed, one pre-existing
  skip; production build and static checks passed. Mocked browser E2E: 41 passed,
  2 real-stack browser E2E passed (two authenticated users, real API and
  PostgreSQL, synthetic audio only).

These counts are evidence for this worktree, not a clean-checkout CI result.
The six full-suite skips must run in CI's exact dependency image as a release
gate. No staging deployment was made: staging currently shares delivery data
and object storage with AR/CL, and a separate isolated mutation environment has
not yet been authorized or provisioned.

## Parser v6: conservative automation boundary

Adversarial review reproduced editorial continuations, unknown commands and
conditions becoming replacement lyrics under the old unquoted-text fallback.
A timestamp or `debe decir` alone does not establish where literal lyric content
ends. Version 6 therefore requires a whole quote-delimited literal (or an
explicit quoted original/new pair) before offering a lyric patch. Positive,
anchored punctuation and layout instructions retain their separate grammar.

This intentionally reduces automation for historical unquoted requests. Their
original source, timestamps and continuation lines remain visible and complete,
but require manual interpretation/editor review. We do not silently add quotes
to customer messages, reinterpret an editorial qualifier as lyric text, or claim
these requests became automatically understood. Explicit quoted full phrases
can still produce contiguous exact-fragment merge proposals requiring review.

`test_change_request_proposals.py` retains actual unquoted timestamp lists and
multiline examples as manual/source-completeness regressions, alongside separate
quoted positive controls for replacements and merges. The migration trades
automatic coverage for content safety; improving unquoted automation later
requires a reviewed literal-selection/confirmation contract, not another list
of words that the parser guesses are instructions.
