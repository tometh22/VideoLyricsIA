# Reviewer shadow experiment — September delivery objective

Target: reduce human lyric/timing review minutes for 600+ September songs,
without introducing errors or hiding false alarms. Stop after lyrics/timing;
backgrounds and video generation require the subsequent stage authorization.

This branch is isolated from the product: no route, worker, deployment,
database mutation, approval, operator publication, training, or automatic apply.
The initial run is a **functional canary**, not a precision evaluation.

## Actual starting point and reuse

Base staging commit: `ec05732c1c8d05340ef7bb26359bf5fbac552912` (#1293).
Read-only snapshot: 300 documents, 299 complete QC, one empty/manual;
277 audio-derived reference hypotheses. Reference availability is not verification.
Semáforo remains uncalibrated and unused for selection. Human edits are not gold
without adjudication. Existing `machine_evidence`, original `EditorVersion`
snapshots and audio/reference revision bindings are exported privately.

Reuse: current CTC implementation/model identity audit; immutable evidence DTOs;
exact segment snapshots; existing content-addressed stems; #1292/#1293
`timing_endpoint_gold.py` for later valid human labels and grouped risk gates.
No production reference policy was changed: Excel enters only this shadow arm.

SOFA is not connected to this canary. Its repository code is MIT, but a compatible,
licensed Spanish checkpoint has not been established here. Do not treat repository
license as checkpoint clearance. See [SOFA](https://github.com/qiuqiao/SOFA) and
[checkpoint sharing](https://github.com/qiuqiao/SOFA/discussions/categories/pretrained-model-sharing).

## Implemented components

| Component | Implementation / boundary |
|---|---|
| XLSX import | `shadow_reference_import.py`: ZIP/XML read only, header discovery, original line breaks/hashes, missing markers separated, unique artist/title candidates, no title-only auto-match |
| Snapshot export | `scripts/export_reviewer_shadow_snapshot.py`: repeatable-read/read-only transaction; original and current segments; no edits |
| Asset export | `scripts/export_reviewer_shadow_assets.py`: selected immutable hypotheses and signed GET URLs; no new Demucs separation |
| Sample | `reviewer_shadow.freeze_sample`: deterministic random+difficult sample, connected artist/audio/recording groups stay within one split; related-version identities still require audit |
| Content proposer/selector | Separate pure functions; two blind audio families required, repeated occurrence checks, no automatic apply; unchanged is not certified correct |
| Audio | `reviewer_shadow_audio.py`: real Whisper-1 on mix and Gemini 2.5 Flash on stem, no candidate/reference/title/artist sent; bounded local clips and explicit usage/errors |
| Timing proposer | Measured RMS pause boundaries, 20 ms frames; **not** phonetic word-end evidence, so insufficient alone for selection |
| Timing selector | Requires target-voice and phonetic support plus synchronization; abstains on locks, ambiguity, missing evidence and render-policy conflict; no model timestamps promoted to trusted endpoints |
| Playback | Private synchronized comparison with byte-range audio, blind-first option, no Genly approve/apply action, local annotation drafts and export |

No current implementation claims an acoustic word-end detector is solved.
CTC/singing alignment and voice attribution remain needed before this timing
selector can produce useful recommendations. Energy pauses are exploratory
candidates, not another universal hold/pitch rule.

## Reproduction

Use Python 3.11 (executed: **3.11.15**), not the system Python 3.9. Do not patch
product code to accommodate the wrong interpreter. Existing experiment environment:
`.context/venvs/shadow-py311/bin/python`. Runtime dependencies used: requests,
google-genai/google-auth, numpy, ffmpeg/ffprobe; SQLAlchemy only in read-only
container exporters. Credentials stay in existing secret files/environment.

From repository root:

```bash
python3.11 -m pytest -o addopts='' -o console_output_style=classic \
  lyricgen/backend/tests/test_shadow_reference_import.py -v --tb=short

PYTHONPATH=lyricgen/backend python3.11 lyricgen/backend/scripts/prepare_reviewer_shadow.py \
  --snapshot /private/snapshot.json --workbook /private/source.xlsx \
  --output /private/experiment --base-commit ec05732c1c8d05340ef7bb26359bf5fbac552912

PYTHONPATH=lyricgen/backend python3.11 lyricgen/backend/scripts/run_reviewer_shadow_canary.py \
  --snapshot /private/snapshot.json --sample /private/experiment/sample.json \
  --references /private/experiment/import.json --canary /private/canary-selection.json \
  --output /private/experiment/canary --commit IMPLEMENTATION_COMMIT

python3.11 lyricgen/backend/scripts/build_reviewer_shadow_preview.py \
  --report /private/experiment/canary/report.json --snapshot /private/snapshot.json \
  --output /private/experiment/canary/reproductor.html

python3.11 lyricgen/backend/scripts/serve_reviewer_shadow_preview.py \
  --directory /private/experiment/canary --port 8767
```

Canary selection contains three `songs` with `job_id`, `case`, exact local `mix`,
existing `stem` and `stem_origin`. It is frozen before calls. Full mix SHA must
match the snapshot. Every clip is PCM mono 16 kHz with SHA and local/global offsets.
Each frozen song has at most four windows of <=24 seconds, 16 calls maximum,
zero automatic retries. Cache identities bind audio, window, policy, prompt and
source revision. Unknown-completion attempts are not silently repurchased.

Whisper configuration follows [official timestamp documentation](https://developers.openai.com/api/docs/guides/speech-to-text#timestamps):
`verbose_json`, word timestamps, no prompt or forced language. Word clocks remain
ASR hypotheses. Gemini receives audio bytes and a fixed blind instruction only.
Gemini reference + another Gemini prompt are still one family.

## Functional canary, 2026-09-05

Frozen sample: 24 songs, 88 distinct windows; 13 train / 6 calibration / 5 test.
Three canary songs are from train/calibration, not the held-out test split.
Canary audio implementation commit: `1e12ea1e7f8b394324ed8936a1f170652071d187`.

| Functional observation | Result |
|---|---:|
| Songs / clips | 3 / 11 (overlapping selection roles were deduplicated) |
| Provider calls attempted | 22 |
| Parsed provider responses | 21 |
| JSON parse failure | 1, retained in the report |
| Content decisions | 1 keep / 10 abstain / 0 propose |
| Timing decisions | 11 abstain / 0 propose |
| Raw measured pause candidates | 12, not selected word-end proposals |
| Human validated recommendations | 0 |

The initial JSON parse error lost raw-response usage inside the adapter and
incorrectly recorded `received_audio=false`. The error occurred after the model
response arrived; the original trace is retained, with this explicit limitation.
A regression test now requires retaining raw response, usage and receipt even
when schema parsing fails. The failed call was not rerun or omitted.

All three mix/stem synchronization tests abstained: low envelope correlation in
at least one probe. Duration differences were ~37–50 ms. This is **not** evidence
of a measured 500 ms drift: a low-correlation lag estimate is inconclusive.
Two Whisper replies were empty. Gemini sometimes returned out-of-clip timestamps;
these are not used as accepted timing. Those limitations explain why successful
network integration is not useful automatic correction yet.

## Verification levels and next decision

1. Pure transformation/source binding tests and real clip/provider evidence: run.
2. Local preview clock and draft persistence: browser smoke run, separate from
   the approved production document/render payload.
3. Real UMG export audio/cartel verification: **not run**; no video produced.

Read-only post-canary audit confirmed all three production revisions/hashes and
approval states unchanged, including Bersuit's 38 locked lines.

Recommendation: **improve before operator suggestions**. Prioritize content
occurrence localization/empty recognition and acoustic target-voice/phonetic
endpoints. Reuse existing blind full-audio evidence before additional purchases.
Do not loosen acceptance thresholds to make a zero-proposal canary look useful.

Human follow-up uses independent blind annotation and a dual-review subset.
Local drafts are NOT valid gold automatically. Validate through the existing
gold contract before comparing A current / B audio / C audio+Excel on the same
songs. Untouched lines, approvals and mouse events are not exact timing gold.
Precision, recall, false-alarm load, control harm, added Excel errors, active
verification/rejection minutes and song-cluster confidence intervals remain
**unmeasured**. No automation/training threshold is certified by this canary.

The preview is an evidence tool, not the final reviewer UX: production should
expose only actionable doubts with immediate audio, editable suggestions,
automatic save, a single song-level approval and task timing. The dominant
decision metric remains human minutes/song including checking false alarms.
