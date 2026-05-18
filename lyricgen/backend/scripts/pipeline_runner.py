#!/usr/bin/env python3
"""Standalone transcription runner that mirrors `_run_transcription_for_job`.

This is the surface the benchmark harness calls — it reproduces the
prod cascade (lrclib synced → lrclib plain → Whisper → hallucination
recovery) WITHOUT the FastAPI / DB / current_user plumbing that
`main.py:_run_transcription_for_job` requires.

It reuses the production pipeline helpers verbatim (no duplicated
logic), so a future cascade tweak in `pipeline.py` is automatically
reflected here. The only thing we don't carry over is the lyrics-
cache write to DB (we pass `db=None` to `_fetch_lrclib`, which falls
back to a no-cache HTTP path).

If `ENABLE_TIER1` / `VALIDATE_SEGMENTS` / `POLISH_TEXT` env flags are
set, the two new Tier-1 helpers (`_validate_segments_against_audio`,
`_polish_segments_text`) run after the base cascade. Their effect is
attached to the returned segments (`seg["flagged"]` and corrected
`seg["text"]`).

NOT intended for production traffic — production goes through the
FastAPI endpoint which adds DB writes, auth, rate limits, etc.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
sys.path.insert(0, str(BACKEND))

from pipeline import (  # noqa: E402
    _fetch_lrclib,
    _lrc_to_segments,
    _audio_duration,
    _verify_lrclib_alignment,
    _detect_hallucination,
    _synthesize_segments_from_plain,
    _align_whisper_to_plain,
    _fill_gaps_with_reference,
    _sanitize_gemini_lyrics,
    transcribe,
)

# Tier 1 helpers are imported lazily so a baseline run never even
# touches their imports — keeps the baseline as pristine as possible.


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def transcribe_local(
    audio_path: str,
    artist: str = "",
    song_title: str = "",
    language: str | None = "es",
    verbose: bool = True,
) -> dict:
    """Run the transcription cascade against a local audio file.

    Returns:
        dict with keys:
          - segments: list[{start, end, text, flagged?}]
          - source: which branch of the cascade produced the segments
                    ("lrclib_synced", "lrclib_plain+whisper", "gemini",
                     "whisper_only", "hallucination_recovered")
          - meta: misc diagnostics (lrclib_dur, user_dur, intro_offset, ...)
    """
    if not os.path.exists(audio_path):
        raise FileNotFoundError(audio_path)

    log = (lambda msg: print(msg)) if verbose else (lambda _msg: None)
    t0 = time.time()
    meta: dict = {"audio_path": audio_path}

    # ─── Step 1: try lrclib first ─────────────────────────────────────
    log(f"[1] lrclib lookup: {artist!r} - {song_title!r}")
    lrc = _fetch_lrclib(artist, song_title, db=None) if (artist or song_title) else None
    user_dur = _audio_duration(audio_path)
    meta["user_dur"] = user_dur

    if lrc:
        synced = lrc.get("synced")
        plain = (lrc.get("plain") or "").strip()
        lrc_dur = lrc.get("duration")
        meta["lrclib_dur"] = lrc_dur
        log(f"    found: synced={'yes' if synced else 'no'}, "
            f"plain={'yes' if plain else 'no'}, dur={lrc_dur}")

        if synced and user_dur and lrc_dur:
            diff = user_dur - lrc_dur
            meta["lrclib_duration_diff"] = diff
            if -15.0 <= diff <= 3.0:
                segs = _lrc_to_segments(synced, audio_duration=user_dur)
                meta["intro_offset"] = 0.0
                return _finalize(segs, "lrclib_synced", meta, audio_path, artist, song_title, log, t0)
            elif 3.0 < diff <= 120.0:
                # User audio has extra intro vs lrclib studio version.
                # Shift all synced timestamps forward by `diff`.
                segs = _lrc_to_segments(synced, audio_duration=user_dur, time_offset=diff)
                meta["intro_offset"] = diff
                return _finalize(segs, "lrclib_synced_with_offset", meta, audio_path, artist, song_title, log, t0)
            else:
                log(f"    synced unusable (dur diff {diff:.1f}s out of range), falling to plain")

        if plain:
            plain = _sanitize_gemini_lyrics(plain)
            return _whisper_anchored_to_plain(
                audio_path, artist, song_title, plain, language, meta, log, t0,
            )

    # ─── Step 2: no lrclib — bare Whisper transcription ──────────────
    log("[2] no lrclib, running raw Whisper")
    return _whisper_only(audio_path, artist, song_title, language, meta, log, t0)


def _whisper_anchored_to_plain(audio_path, artist, song_title, plain, language, meta, log, t0):
    """Run Whisper with plain lyrics as primer; if it hallucinates,
    re-synthesize segments from the plain lyrics anchored to whatever
    Whisper output we did get."""
    log("[1b] lrclib plain found — Whisper with priming + anchoring")
    segs = transcribe(audio_path, language=language, lyrics_hint=plain[:800])
    user_dur = meta.get("user_dur")
    hallucinated, reason = _detect_hallucination(segs, user_dur, language=language)
    meta["whisper_hallucinated"] = bool(hallucinated)
    meta["whisper_hallucination_reason"] = reason
    if hallucinated:
        log(f"    Whisper hallucinated ({reason}) — synthesizing from plain")
        anchors = _align_whisper_to_plain(segs, plain)
        segs = _synthesize_segments_from_plain(
            plain.splitlines(), anchors, user_dur or 0.0, start_time=0.0,
        )
        return _finalize(segs, "hallucination_recovered_from_plain", meta, audio_path, artist, song_title, log, t0)

    # Whisper output is plausible; optional gap fill if coverage low
    return _finalize(segs, "lrclib_plain+whisper", meta, audio_path, artist, song_title, log, t0)


def _whisper_only(audio_path, artist, song_title, language, meta, log, t0):
    segs = transcribe(audio_path, language=language, lyrics_hint=None)
    user_dur = meta.get("user_dur")
    hallucinated, reason = _detect_hallucination(segs, user_dur, language=language)
    meta["whisper_hallucinated"] = bool(hallucinated)
    if hallucinated:
        log(f"    Whisper hallucinated ({reason}) — no plain reference to recover from")
        meta["whisper_hallucination_reason"] = reason
    return _finalize(segs, "whisper_only", meta, audio_path, artist, song_title, log, t0)


def _finalize(segs, source, meta, audio_path, artist, song_title, log, t0):
    """Apply Tier 1 opt-in improvements then return the bundle."""
    if _env_truthy("ENABLE_TIER1") or _env_truthy("POLISH_TEXT"):
        from pipeline import _polish_segments_text  # lazy import; helper added behind flag
        polished = _polish_segments_text(segs, artist, song_title)
        if polished is not None and polished is not segs:
            log(f"    [tier1] polished {sum(1 for a, b in zip(segs, polished) if a.get('text') != b.get('text'))} segment text(s)")
            segs = polished
    if _env_truthy("ENABLE_TIER1") or _env_truthy("VALIDATE_SEGMENTS"):
        from pipeline import _validate_segments_against_audio
        flagged = _validate_segments_against_audio(audio_path, segs, job_id=None)
        if flagged is not None and flagged is not segs:
            n_flagged = sum(1 for s in flagged if s.get("flagged"))
            log(f"    [tier1] flagged {n_flagged}/{len(flagged)} segment(s) as suspicious")
            segs = flagged

    elapsed = time.time() - t0
    log(f"DONE source={source} segments={len(segs)} elapsed={elapsed:.1f}s")
    return {
        "segments": segs,
        "source": source,
        "meta": meta,
        "elapsed_seconds": elapsed,
    }


if __name__ == "__main__":
    # Quick CLI smoke test
    import argparse
    p = argparse.ArgumentParser(description="Run transcribe_local() against one audio file")
    p.add_argument("audio_path")
    p.add_argument("--artist", default="")
    p.add_argument("--title", default="")
    p.add_argument("--lang", default="es")
    args = p.parse_args()
    result = transcribe_local(args.audio_path, args.artist, args.title, args.lang)
    import json
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
