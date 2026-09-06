# Excel-guided campaign reconciliation

Scope: existing campaign ba3318bdfffe, exact 300 IDs. No new paid calls, timing
rules, source edits, approvals, background generation or renders.

The September 6 snapshot is read-only and includes the user's Polizonte review
11, status lyrics_approved. It is protected, not an inference baseline to edit.

The Art Tracks-4 workbook contains 154 actual lyric cells, 133 source-link cells
and 7 absence markers. Previous 287-present/271-usable reporting incorrectly
counted link text. There are 145 associated actual lyric references. Links stay
human pointers; the importer never downloads them. Workbook bytes are unchanged.

## Method

- Whole-song sequence alignment and local, adjacent phrase anchors enumerate
  discrepancies. Neither reference matching nor forced alignment proves lyrics.
- Candidates require the complete changed caption to occur uniquely in both
  cached blind original-mix families, with provider intervals overlapping that
  caption and no neighboring caption. Source audio identity and prompt lineage
  must match; overlapping windows from one family count once.
- No deletions from substring presence: hearing the retained words does not
  prove other words were absent. Written accents require orthographic evidence,
  not acoustic agreement with an unaccented Excel cell.
- Human-modified, locked and approved content is never changed. Existing
  machine candidates are preserved separately from newly generated repairs.
- Same-source candidate generations use immutable content-addressed artifacts
  and a campaign metadata pointer. Old objects remain intact; removing the
  pointer restores legacy lookup. No artificial editor revision bump is used.

The first offline audit exposed deletion hypotheses that were merely substrings
of the baseline. They were discarded before publication, and a regression test
now forbids that inference. No threshold tuning used reserved evaluation cases;
each method is frozen by code SHA before execution. Results are operational
development evidence, not independent accuracy or time-saving measurements.

## Execution

Python 3.11; PYTHONPATH=lyricgen/backend. Run:

```
python lyricgen/backend/scripts/audit_campaign_excel.py --root ARTIFACT_ROOT \
  --snapshot CURRENT_READONLY_SNAPSHOT --output NEW_OUTPUT_DIRECTORY \
  --commit IMPLEMENTATION_COMMIT --align-new
```

Set HF_HUB_OFFLINE=1 and TRANSFORMERS_OFFLINE=1 for local alignment. The runner
has no paid-call adapter. It writes separate per-ID audit/candidate/review JSON,
with source hashes, provider receipts, localized doubts, observed latency and
zero-new-call accounting. The source snapshot, legacy results and workbook are
never overwritten. Output directories should be unique per execution.

## Release boundary

Staging release is authorized for the feature, but another staging merge is
blocked on the discovered production Sentinel autodeploy coupling. Do not merge
or alter production configuration without the user's explicit resolution.
Existing staging remains 142e644d / 1.1.1 until then. Publication of the new
generation must use the deployed native publisher with live source checks and
before/after document/approval preservation verification. Do not claim local
candidate generation means the replacement is already visible in Campañas.
