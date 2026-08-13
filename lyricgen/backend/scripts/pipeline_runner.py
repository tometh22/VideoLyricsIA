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


_DB_STUBS_INSTALLED = False


def _install_db_stubs():
    """Stub out DB-touching helpers so `_run_transcription_for_job` can run
    outside FastAPI without a real Postgres connection. Idempotent; safe to
    call repeatedly. Used by `transcribe_local_via_main` and the QA suite.

    Stubs (matching the helpers actually invoked by the cascade):
      - `database.SessionLocal()` → FakeSession (no-op everything, query→None)
      - `main.scoped_db()` → contextmanager that yields None
      - `jobs.set_timing_source()` → records into module-level last_telemetry
      - `jobs.update_job()` → records into module-level last_telemetry
    """
    global _DB_STUBS_INSTALLED, _LAST_TELEMETRY
    if _DB_STUBS_INSTALLED:
        return
    # Force a DSN that creates the engine lazily (never connects).
    os.environ.setdefault("DATABASE_URL", "postgresql://x@localhost:1/x")

    import database  # noqa: E402
    class _Q:
        def filter(self, *a, **kw): return self
        def filter_by(self, *a, **kw): return self
        def first(self): return None
        def all(self): return []
        def one_or_none(self): return None
        def order_by(self, *a, **kw): return self
        def limit(self, *a, **kw): return self
    class _FakeSession:
        def query(self, *a, **kw): return _Q()
        def add(self, *a, **kw): pass
        def merge(self, *a, **kw): pass
        def commit(self): pass
        def rollback(self): pass
        def close(self): pass
        def flush(self): pass
        def execute(self, *a, **kw): return _Q()
        def expire_all(self): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
    database.SessionLocal = lambda: _FakeSession()

    from contextlib import contextmanager
    @contextmanager
    def _stub_scoped_db():
        yield None
    import main as _main
    _main.scoped_db = _stub_scoped_db

    import jobs
    def _stub_set_timing(job_id, src):
        _LAST_TELEMETRY["timing_source"] = src
        _LAST_TELEMETRY["set_timing_source_calls"].append((job_id, src))
    def _stub_update_job(job_id, **kwargs):
        _LAST_TELEMETRY["update_job_calls"].append((job_id, dict(kwargs)))
        cs = kwargs.get("current_step")
        if cs is not None:
            _LAST_TELEMETRY["steps"].append((cs, kwargs.get("progress")))
    jobs.set_timing_source = _stub_set_timing
    jobs.update_job = _stub_update_job

    _DB_STUBS_INSTALLED = True


# Captures telemetry from the most recent `transcribe_local_via_main` call.
# Reset at the start of each call; safe in single-threaded serial execution.
_LAST_TELEMETRY: dict = {}


def transcribe_local_via_main(
    audio_path: str,
    artist: str = "",
    title: str = "",
    language: str | None = "es",
    job_id: str = "qa-local",
) -> dict:
    """Run the FULL post-PR-G cascade in `main._run_transcription_for_job`
    against a local audio file. DB is stubbed. Returns:

        {
          segments, source, recovery_source, reference_lyrics,
          meta: {audio_path, audio_duration_s, artist, title},
          telemetry: {
            timing_source, steps, set_timing_source_calls,
            update_job_calls, elapsed_seconds,
          },
        }

    This is the entry point used by the QA suite (`scripts/qa/`). It tests
    the FULL audio-as-truth flow (FA → whisperX → reconcile → recovery)
    rather than the legacy lrclib_synced fast-path that `transcribe_local`
    above reproduces.
    """
    import asyncio
    global _LAST_TELEMETRY
    _LAST_TELEMETRY = {
        "timing_source": None,
        "steps": [],
        "set_timing_source_calls": [],
        "update_job_calls": [],
        "elapsed_seconds": None,
    }
    _install_db_stubs()
    import main as _main

    t0 = time.time()
    try:
        result = asyncio.run(_main._run_transcription_for_job(
            request=None,
            current_user={"id": 2, "tenant_id": "qa@local"},
            job_id=job_id,
            audio_path=audio_path,
            language=language or "",
            artist=artist,
            title=title,
            filename=os.path.basename(audio_path),
        ))
    except Exception as e:
        elapsed = time.time() - t0
        return {
            "segments": [],
            "source": None,
            "recovery_source": None,
            "reference_lyrics": "",
            "meta": {
                "audio_path": audio_path,
                "artist": artist, "title": title,
                "error": repr(e),
            },
            "telemetry": {**_LAST_TELEMETRY, "elapsed_seconds": elapsed},
        }
    elapsed = time.time() - t0
    _LAST_TELEMETRY["elapsed_seconds"] = elapsed
    audio_dur = None
    try:
        audio_dur = _audio_duration(audio_path)
    except Exception:
        pass
    return {
        "segments": result.get("segments") or [],
        "source": _LAST_TELEMETRY.get("timing_source"),
        "recovery_source": result.get("recovery_source"),
        "reference_lyrics": result.get("reference_lyrics") or "",
        "meta": {
            "audio_path": audio_path,
            "artist": artist, "title": title,
            "audio_duration_s": audio_dur,
        },
        "telemetry": dict(_LAST_TELEMETRY),
    }


if __name__ == "__main__":
    # Quick CLI smoke test
    import argparse
    p = argparse.ArgumentParser(description="Run transcribe_local() against one audio file")
    p.add_argument("audio_path")
    p.add_argument("--artist", default="")
    p.add_argument("--title", default="")
    p.add_argument("--lang", default="es")
    p.add_argument("--via-main", action="store_true",
                   help="Use the new post-PR-G cascade (transcribe_local_via_main) "
                        "instead of the legacy transcribe_local.")
    args = p.parse_args()
    if args.via_main:
        result = transcribe_local_via_main(args.audio_path, args.artist, args.title, args.lang)
    else:
        result = transcribe_local(args.audio_path, args.artist, args.title, args.lang)
    import json
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
