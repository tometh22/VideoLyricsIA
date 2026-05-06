"""Smoke test for the intro-trim hypothesis on "El Plan de la Mariposa - El Riesgo".

Why: the YouTube "Video Oficial" version of this song is 5:42, but the lrclib
studio version is 4:29 (-73s). The first ~73s of the user's MP3 is intro
material the studio version doesn't have. Hypothesis: those extra 73s are
poisoning Whisper's context — feeding the trimmed body should let Whisper
return clean segments instead of 3 hallucinated rows.

Run locally (NOT in production):
    cd lyricgen/backend
    source venv/bin/activate
    export OPENAI_API_KEY=sk-...
    python scripts/test_intro_trim.py

Cost: 2 Whisper calls × ~$0.03 = ~$0.06 total.
"""

import os
import sys
import tempfile
import subprocess

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import (
    _fetch_lrclib,
    _audio_duration,
    _detect_hallucination,
    _has_fuzzy_intra_loop,
    _transcribe_via_openai_api,
)

MP3 = os.environ.get(
    "TEST_MP3",
    os.path.expanduser("~/Downloads/El Plan de la Mariposa - El Riesgo ( Video Oficial ).mp3"),
)
ARTIST = os.environ.get("TEST_ARTIST", "El Plan de la Mariposa")
SONG = os.environ.get("TEST_SONG", "El Riesgo")


def trim_audio(input_path: str, start_seconds: float, output_path: str) -> None:
    """ffmpeg -ss <start> -i input -c copy output. Stream copy is fast and
    keeps the original encoding so we're not re-encoding for the test.
    """
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-ss", str(start_seconds),
            "-i", input_path,
            "-c", "copy",
            "-loglevel", "error",
            output_path,
        ],
        check=True,
    )


def summarize(segments, audio_dur, label):
    print(f"\n=== {label} ===")
    hall, reason = _detect_hallucination(segments, audio_dur)
    print(f"  segments: {len(segments)}")
    print(f"  hallucinated: {hall}" + (f" — {reason}" if reason else ""))
    if segments:
        print(f"  first: [{segments[0]['start']:.1f}s] {segments[0]['text'][:80]!r}")
        if len(segments) > 1:
            print(f"  last:  [{segments[-1]['start']:.1f}s] {segments[-1]['text'][:80]!r}")
        looped = sum(1 for s in segments if _has_fuzzy_intra_loop(s.get("text") or ""))
        if looped:
            print(f"  WARNING: {looped} segment(s) contain intra-loops")
    return hall


def main() -> int:
    if not os.environ.get("OPENAI_API_KEY"):
        print("ERROR: OPENAI_API_KEY env var not set. Export it before running.")
        return 1
    if not os.path.exists(MP3):
        print(f"ERROR: MP3 not found at {MP3}")
        print("  Set TEST_MP3=/path/to/file.mp3 to override.")
        return 1

    print(f"MP3: {MP3}")
    user_dur = _audio_duration(MP3)
    print(f"User audio duration: {user_dur:.1f}s ({user_dur / 60:.1f} min)")

    print(f"\nFetching lrclib for {ARTIST!r} - {SONG!r}...")
    lrc = _fetch_lrclib(ARTIST, SONG)
    if not lrc or not lrc.get("plain"):
        print("ERROR: lrclib miss. Cannot test trim hypothesis.")
        return 1

    plain = lrc["plain"]
    lrc_dur = lrc.get("duration") or 0
    diff = user_dur - lrc_dur
    print(f"lrclib duration: {lrc_dur:.1f}s | diff: {diff:+.1f}s")
    print(f"lrclib plain: {len(plain)} chars, {len([l for l in plain.splitlines() if l.strip()])} lines")

    if abs(diff) <= 3:
        print("\nNo significant duration mismatch — trim wouldn't apply on prod.")
        print("Running baseline only.")
        segs = _transcribe_via_openai_api(MP3, language="es", lyrics_hint=plain)
        summarize(segs, user_dur, "FULL AUDIO (no trim)")
        return 0

    # ─── BASELINE: full audio ──────────────────────────────────────────
    print(f"\nCalling Whisper on FULL audio ({user_dur:.0f}s)...")
    segs_full = _transcribe_via_openai_api(MP3, language="es", lyrics_hint=plain)
    bad_full = summarize(segs_full, user_dur, f"BASELINE — full audio ({user_dur:.0f}s)")

    # ─── TRIMMED: skip the diff seconds ────────────────────────────────
    print(f"\nTrimming {diff:.0f}s of intro audio...")
    tmp_dir = tempfile.mkdtemp()
    trimmed = os.path.join(tmp_dir, "trimmed.mp3")
    trim_audio(MP3, diff, trimmed)
    trimmed_dur = _audio_duration(trimmed)
    print(f"Trimmed audio duration: {trimmed_dur:.1f}s")

    print(f"Calling Whisper on TRIMMED audio ({trimmed_dur:.0f}s)...")
    segs_trim = _transcribe_via_openai_api(trimmed, language="es", lyrics_hint=plain)
    bad_trim = summarize(segs_trim, trimmed_dur, f"TRIMMED — body only ({trimmed_dur:.0f}s)")

    # Cleanup
    try:
        os.unlink(trimmed)
        os.rmdir(tmp_dir)
    except OSError:
        pass

    # ─── Verdict ──────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("VERDICT")
    print("=" * 60)
    print(f"  Full audio:    {len(segs_full):3d} segments — {'BAD' if bad_full else 'OK'}")
    print(f"  Trimmed audio: {len(segs_trim):3d} segments — {'BAD' if bad_trim else 'OK'}")
    if not bad_trim and bad_full:
        print("\n  ✅ Trim hypothesis CONFIRMED. Implementing it on the plain")
        print("     branch will fix this song (and likely other 'Video Oficial'")
        print("     uploads with extra intro audio).")
    elif bad_trim and bad_full:
        print("\n  ❌ Trim alone is NOT enough. Whisper still hallucinates on the")
        print("     trimmed body. The recover branch is still our best fallback.")
    elif not bad_full:
        print("\n  ⚠️  Full audio worked this run (Whisper non-deterministic).")
        print("     Run again to re-test.")
    else:
        print("\n  Trimmed worked, full would have too. Trim is harmless extra safety.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
