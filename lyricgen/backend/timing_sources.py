"""Centralized ontology for `jobs.timing_source` values.

Every successful run of `_run_transcription_for_job` MUST emit one of
`VALID_TIMING_SOURCES`. `timing_source=NULL` is exclusively reserved for
jobs processed before this registry existed (pre-2026-05-24) — any
fresh emission with NULL is a Bug D regression (orphan return path).

Constraint: `database.Job.timing_source` is VARCHAR(20). Every value in
`VALID_TIMING_SOURCES` must be ≤ 20 chars; `test_timing_sources.py`
asserts this. Adding a new value? Pick a short name (≤ 20) and add a
test if the path is observable in the QA suite.

The deprecated set is the regression guard for PR-G WorldClass (the
audio-as-truth refactor): `lrclib_synced*` were timing sources before
PR-G eliminated them. `lrclib_plain` was historically a recovery_source
mistakenly logged as timing_source in some paths; explicit ban so we
never re-introduce the cross-contamination.
"""
from __future__ import annotations

# Rotor-grade ±50ms alignment of known lyrics to the audio (lrclib text
# or Gemini reference passed through cureau forced aligner).
FORCED_ALIGN = "forced_align"

# whisperX word-level timing + canonical reference text from lrclib/Gemini.
# The reconcile step re-buckets whisperX words into the reference's line
# structure, giving timing-from-audio with orthography-from-reference.
WHISPERX_RECONCILED = "whisperx_reconciled"

# whisperX standalone — used when there's no reference to reconcile
# against (no-lyrics path) or when reconcile failed but whisperX is
# plausibly correct on its own.
WHISPERX = "whisperx"

# Whisper-1 (OpenAI API) + lrclib plain text as `initial_prompt` hint.
# Happy path of the lrclib-plain branch: timing from Whisper, text
# corrected by the hint. No hallucination recovery needed.
WHISPER_LRCLIB = "whisper_lrclib"

# Whisper-1 + lrclib plain + hallucination recovery. The Whisper output
# hallucinated, `_detect_hallucination` fired, and we synthesised line
# segments from the lrclib plain text using whatever time anchors we
# could pull out. Audibly OK, observability-degraded.
WHISPER_LRCLIB_REC = "whisper_lrclib_rec"

# Whisper-1 + Gemini/lyrics.ovh reference + gap-fill recovery. Analog
# of WHISPER_LRCLIB_REC but the reference text came from Gemini
# grounding (or lyrics.ovh) instead of lrclib.
WHISPER_GEMINI_REC = "whisper_gemini_rec"

# Bare Whisper-1, no reference. Last resort — emitted when every other
# path failed (no lrclib, no Gemini, no whisperX). UX is whatever the
# raw ASR produced; this tag at least makes the degradation visible.
WHISPER_RAW = "whisper_raw"

# whisperX wordstamps + canonical text from lrclib + relative-timing
# hint from lrclib syncedLyrics. Third fallback in the audio-as-truth
# path (used when both reconcile and forced_align fail). lrclib_aligner
# rebuckets whisperX words against the canonical line structure and
# fills unmatched lines using the lrclib synced timestamps with a
# global offset computed from matched whisperX anchors. Less precise
# than reconcile/FA timing (~2-5s on interpolated lines) but recovers
# cases where cureau crashes on highly-repetitive lyrics (incident
# 2026-05-26: Sin Gamulán, Mujer Amante — see issue #357).
WHISPERX_LRCLIB = "whisperx_lrclib"


VALID_TIMING_SOURCES = frozenset({
    FORCED_ALIGN,
    WHISPERX_RECONCILED,
    WHISPERX,
    WHISPERX_LRCLIB,
    WHISPER_LRCLIB,
    WHISPER_LRCLIB_REC,
    WHISPER_GEMINI_REC,
    WHISPER_RAW,
})


# Values that the system MUST NOT emit any more — PR-G regression guards.
# The QA suite (golden_set.yaml + _detectors.py) already enforces these,
# but the Python set lets `_emit_segments` defend at runtime too.
DEPRECATED_TIMING_SOURCES = frozenset({
    # PR-G WorldClass eliminated lrclib_synced — see incident "Cosas Mías"
    # (2026-05-22, agus.cafisi): a lrclib_synced job ran 35 s off, the
    # operator filed a bug, and the whole audio-as-truth rewrite followed.
    "lrclib_synced",
    "lrclib_synced_with_offset",
    # Historic mislabel: `lrclib_plain` was the value of `recovery_source`
    # in some paths, never a legitimate timing_source. Banning the string
    # prevents future cross-contamination.
    "lrclib_plain",
})
