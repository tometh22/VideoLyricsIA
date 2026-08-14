# Changelog

All notable changes to VideoLyricsIA (GenLy AI) are documented here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
