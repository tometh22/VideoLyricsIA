# Lyrics quality benchmark report

Scored 6 job(s) under `benchmark/dataset/`

## Per-job results

| Job | Source | WER baseline → tier1 | AOO mean (s) baseline → tier1 | Composite baseline → tier1 |
|---|---|---|---|---|
| `0181c30e0fbb` | `lrclib_synced` | 0.000 → 0.000 | 40.992 → 40.992 | 0.500 → 0.500 |
| `1c74d0fd1532` | `lrclib_plain+whisper` | 0.195 → 0.195 | 17.730 → 17.730 | 0.402 → 0.402 |
| `2144aacb453e` | `lrclib_synced` | 0.160 → 0.160 | 59.639 → 59.639 | 0.420 → 0.420 |
| `677c5f0dce09` | `lrclib_synced` | 0.000 → 0.015 | 27.629 → 27.629 | 0.500 → 0.492 |
| `6c63e9aedbec` | `lrclib_synced` | 0.043 → 0.054 | 50.632 → 50.632 | 0.479 → 0.473 |
| `8b9b19db22e9` | `lrclib_synced` | 0.006 → 0.006 | 36.371 → 36.371 | 0.497 → 0.497 |

## Aggregates

- Baseline mean WER: **0.067** (6 jobs)
- Baseline mean AOO: **38.832 s**
- Baseline mean composite: **0.466**

- Tier-1 mean WER: **0.072** (Δ = +0.4%)
- Tier-1 mean AOO: **38.832 s** (Δ = +0 ms)
- Tier-1 mean composite: **0.464** (Δ = -0.2%)

## Decision (per plan thresholds)

- WER dropped -6.6% (target: ≥30%)
- AOO dropped 0.0% (target: ≥40%)

### Verdict: ❌ **Do not ship** — re-tune prompts/thresholds before any deploy
