# QA suite — lyric transcription pipeline (post Rotor-Killer + PR-G)

End-to-end QA for the post-PR-G audio-as-truth architecture. Runs the
real pipeline (`_run_transcription_for_job`) against a curated golden set
of songs, captures per-song artifacts (segments + logs + telemetry), and
emits a markdown report with PASS/WARN/FAIL verdict + exit code.

## When to run

- **Before promoting flags to prod.** Run `--mode full` and require a PASS
  verdict (or a knowing WARN).
- **After any change to `_run_transcription_for_job` or its helpers.** Run
  `--mode quick` for a 10-minute sanity check.
- **Weekly cron.** Same as pre-prod gate; sends a markdown report.

## What it tests

14 detectors (see `_detectors.py`) catch known incident shapes and
regression risks. The most important ones:

- `timing_source_in_allowlist` — actual must match `expected.timing_source`.
- `timing_source_not_in_deny` — `lrclib_synced` is FORBIDDEN after PR-G.
- `mega_segment_hallucination` — caught "El Arbol" (346s/3 words).
- `uniform_synthesizer_fallback` — caught original "Cosas Mías" (uniform 7s).
- `max_line_dur_exceeded` — Rotor parity, ceiling 8s per line.
- `missing_word_stamps` — required for karaoke + confidence highlighting.
- ...plus 8 more WARN-tier checks.

## Run modes

```bash
# Quick (4 songs, no delays, ~10 min, ~$0.20)
python scripts/qa/run_qa_suite.py --mode quick

# Full (7 songs, 30s delays, ~45 min, ~$0.40)
python scripts/qa/run_qa_suite.py --mode full

# Dry-run (no Replicate; checks YAML + audio file presence)
python scripts/qa/run_qa_suite.py --mode full --dry-run

# Single song
python scripts/qa/run_qa_suite.py --only los_abuelos_cosas_mias

# Re-analyze without re-executing
python scripts/qa/analyze_run.py --run-dir ~/.gstack/projects/VideoLyricsIA/qa-runs/<ts>/
```

The runner needs:
- Replicate token (for FA / whisperX / demucs)
- OpenAI key (for Whisper-1 verify slices)
- Vertex creds (for Gemini lyrics fetch on songs without lrclib)

Easiest invocation that injects all of them is via the api service:
```bash
railway environment staging
railway run --service api ./venv/bin/python scripts/qa/run_qa_suite.py --mode quick
```

## Artifacts

Every run writes to `~/.gstack/projects/VideoLyricsIA/qa-runs/<ts>/`:

- `golden_set.yaml` — snapshot of the expected profiles used.
- `meta.json` — git sha, env flags, started/finished timestamps.
- `runs/<song_id>.json` — full segments + telemetry + meta.
- `runs/<song_id>.log` — captured pipeline logs (incl. all engine markers).
- `summary.json` — pre-analysis aggregates (timing_source distribution, p50/p95 elapsed).
- `analysis.json` — every detector's verdict per song.
- `report.md` — human-readable: failures up top, then warnings, then per-song detail.

## Reading the report

- 🟢 PASS — every song's detectors are PASS. Verdict exit 0.
- 🟡 WARN — at least one WARN, zero FAILs. Verdict exit 0 (banner).
- 🔴 FAIL — at least one FAIL or ERROR. Verdict exit 1 (gates CI).

When you see a FAIL, the report's top section names the detector and links
to the relevant log file. The `details:` dict carries the concrete numbers
(e.g., `actual: 'lrclib_synced', forbidden: [lrclib_synced]`).

## Updating expectations

When the pipeline architecture changes (e.g., a new timing_source value),
update `golden_set.yaml`:

- Add the new value to `expected.timing_source` allow-lists where it can
  legitimately occur.
- Update `defaults.expected.forbidden_timing_sources` if a path is being
  retired (like we did with `lrclib_synced` after PR-G).
- Bump `schema_version` if the grammar changes (not just new songs).

## Adding songs

Add an entry under `songs:` in `golden_set.yaml`:

- `id`: kebab-case stable identifier (used for artifact filenames).
- `class: [quick, full]` to include in both modes, or `[full]` for full only.
- `path`: absolute path to a local audio file.
- `expected`: at minimum `timing_source`, `first_vocal_s_range`,
  `n_lines_range`, `max_line_dur_s`, `max_elapsed_s`. The defaults block
  fills in `must_have_words`, `forbidden_timing_sources`, etc.

## Known limits

- **Replicate 429 throttle**: if the account is flagged `< $5 credit`,
  parallel demucs+FA requests are throttled. Mitigation: serial mode +
  `--delay-s 30` default. Tune up if needed.
- **DB stubbed**: lrclib + Gemini caches don't persist across runs; first
  request always pays the network round-trip (~0.5-2s extra per song).
- **Cost estimator is best-effort** (greps logs for engine markers). The
  Replicate billing dashboard remains the source of truth for absolute
  spend.
