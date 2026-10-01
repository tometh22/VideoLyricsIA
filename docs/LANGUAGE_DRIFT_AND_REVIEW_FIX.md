# Initial language and reviewer fixes — 1.1.9

Scope: staging only, PR #1310. No retrospective reprocessing, song approval,
editorial writes, timing recovery, new Gemini calls or LoRA changes.

## Confirmed causes and changes

The parallel batch path starts blind WhisperX while the complete-audio reference
is pending. Its initial language resolver sees an empty reference and is skipped.
A subsequent reference rejection also erased the diagnostic reference in the
emitted result. Auto studio recognition now consumes that existing task before
language selection. This serializes those two stages, without extra calls;
explicit-language and live Auto paths retain their scheduling. Lyrics remain
excluded from the ASR prompt. Bilingual evidence, including a short English verse
hidden by a Spanish majority, vetoes a global hint. Missing or uncertain evidence
still falls back to provider-auto. This cannot guarantee detection of a verse
that the reference itself omitted.

The pinned WhisperX provider defaults to `task=transcribe`, not `translate`;
the payload now states that explicitly. Raw stored output already contains the
foreign phrases. These are recognition errors, not an editor translation action.

Discrepancy detection retains the complete-audio hypothesis after rejection and
catches local repetitive drift. Detection does not establish the correct text.

The editor's reference matcher accepted 30% bag-of-word overlap and combined
unrelated lines. It also indexed suggestions by original array position while
the editor used stable IDs. Tango replay reproduces a suggestion from Gardel to
Dios and two entirely unrelated phrase substitutions. The replacement only
suggests a single unambiguous orthographic edit, preserves word count/order,
never removes accents, and keys suggestions by the current edited line identity.
Warnings and audio/reference inspection remain available when no correction is
supported. This does not certify reference suggestions as acoustically correct.

Approval formerly stopped on a stale client flag with a toast, while the only
resolution button was above the timeline. The callback fetched a server revision
without first flushing the editor and silently logged failures. Approval now
opens a visible human confirmation dialog; the fixed footer also exposes it.
Confirmation flushes the current edits first, submits that exact saved revision,
and reports failed saves or stale revisions. The backend gate remains required;
no automatic approval or blanket waiver is introduced.

## Evidence

Four authorized Whisper-1 calls on 24-second original-mix clips, Spanish, no
prompt, no retries. Existing Gemini evidence reused. Provider receipts are in
Riyadh `.context/reviewer-shadow-artifacts/language-repair-four/`.

- Pulpito 108–132: repetitive vocalization output remains unreliable.
- Pulpito 126–150: partial Spanish phrase and repetitions; not a validated repair.
- Cambiar el mundo 90–114: Spanish phrase hypothesis; cached Auto already returned
  Spanish here, so this does not establish an improvement from specifying language.
- Cambiar el mundo 270–294: Spanish instead of cached Auto Italian, but completeness
  and boundaries are not certified. This is a bounded alternative, not a repaired
  or approved song. The existing full-song WhisperX baseline was also erroneous.

Mi Nena additionally has stored Whisper-1 Spanish words agreeing with the
Gemini hypothesis where WhisperX produced English. This evidence diagnoses the
selection failure; it is not automatically copied into Agus's document.

No completed song-level transcription improvement or reduction in reviewer
minutes is claimed. Pulpito remains unresolved. Tests cover language selection,
short bilingual veto, reference failure, diagnostic preservation, suggestion
identity and human resolution save/conflict/error paths. Full CI and served
staging verification are required for the release.
