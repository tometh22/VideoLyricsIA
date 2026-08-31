# Canonical raw cohort decision — 2026-08-31

Decision: **23 songs with `raw_quality == "exact"` are the canonical cohort
for every metric or gate that depends on pre-human pipeline output.**

`reconstructed` remains a useful provenance label and its artifacts remain
available for qualitative diagnosis, but it is not equivalent to an immutable
pre-human checkpoint and cannot enter a numerical gate.

## Independent reproduction

The five songs that contain both an immutable `original_segments` checkpoint
and enough audit events to rewind were reconstructed again with the repository
implementation and compared to the exact checkpoint using the official text
normalization.

| Case | Rewind WER | Exact lines | Rewind lines | Maximum boundary drift |
|---|---:|---:|---:|---:|
| `c54adb6de148` | 1.9% | 42 | 43 | 5.130 s |
| `ffa93697b8fb` | 18.2% | 27 | 29 | 4.322 s |
| `ad2a155868e6` | 7.2% | 51 | 51 | 1.520 s |
| `195f117a2a2b` | 2.9% | 44 | 43 | 21.680 s |
| `24a8c89b36b5` | 28.9% | 39 | 39 | 29.337 s |

Median rewind WER is 7.2%. Four of five controls change lexical text and three
change line count. Most decisively, `c54adb6de148` reports zero truncated and
zero invalid audit indices, yet gains a line and drifts 5.130 seconds. The
rewind therefore has unobserved information loss; its own status cannot certify
that a reconstruction is exact.

The manifest preserves the original quality counts (23 exact, 18
reconstructed, 16 estimated, 8 none) so provenance is not destroyed. The
central `eval.raw_cohort.RAW_TRUSTED` definition, not relabeling, enforces the
23-song gate cohort.

## Consequences

- Metrics previously reported on exact + reconstructed (41) are historical
  diagnostics, not current gates.
- Edit-effort events remain valid observations of operator work; only an
  inferred pre-human state is excluded.
- Timing work targets final edges only: on the exact cohort, start p50/p90 is
  0/0 ms, while 95 of 848 lines (11.2%) end early, with a tail to 8.407 s.
