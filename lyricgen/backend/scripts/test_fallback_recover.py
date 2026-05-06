"""End-to-end smoke for the Gemini-fallback recovery on El Plan de la
Mariposa - El Riesgo (Video Oficial).mp3.

Simulates EXACTLY the path /transcribe takes when lrclib fails:
    1. Run Whisper on the full audio with reference text as
       lyrics_hint (mimics what main.py does at line 1058 ish).
    2. Detect hallucination on the full output.
    3. Run _fill_gaps_with_reference on (segments, reference).
    4. Print the merged output.

If the merged output starts with the spoken-intro Whisper segment
(real timestamps) and continues with reference lines spread across the
remaining audio, the production behavior is correct. If we still see
just 3 raw Whisper rows, the per-segment filter still has a hole.

Run:
    cd lyricgen/backend
    source venv/bin/activate
    export OPENAI_API_KEY=sk-...
    export GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa.json
    export VERTEX_PROJECT=...
    python scripts/test_fallback_recover.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import (
    _audio_duration,
    _detect_hallucination,
    _fetch_lrclib,
    _fill_gaps_with_reference,
    _has_fuzzy_intra_loop,
    _transcribe_via_openai_api,
)

MP3 = os.environ.get(
    "TEST_MP3",
    os.path.expanduser("~/Downloads/El Plan de la Mariposa - El Riesgo ( Video Oficial ).mp3"),
)
ARTIST = os.environ.get("TEST_ARTIST", "El Plan de la Mariposa")
SONG = os.environ.get("TEST_SONG", "El Riesgo")


def main() -> int:
    if not os.environ.get("OPENAI_API_KEY"):
        print("ERROR: OPENAI_API_KEY not set"); return 1
    if not os.path.exists(MP3):
        print(f"ERROR: MP3 not found at {MP3}"); return 1

    print(f"MP3: {MP3}")
    user_dur = _audio_duration(MP3)
    print(f"Audio: {user_dur:.1f}s\n")

    print("Fetching lrclib (for reference text only — simulating fallback)...")
    lrc = _fetch_lrclib(ARTIST, SONG)
    if not lrc or not lrc.get("plain"):
        print("ERROR: lrclib unavailable; can't simulate fallback"); return 1
    reference = lrc["plain"]
    ref_lines = [l for l in reference.splitlines() if l.strip()]
    print(f"Reference: {len(reference)} chars, {len(ref_lines)} lines\n")

    print("Calling Whisper on FULL audio (mimics fallback path)...")
    segments = _transcribe_via_openai_api(MP3, language="es", lyrics_hint=reference)
    print(f"\nWhisper returned: {len(segments)} segment(s)")
    for i, s in enumerate(segments):
        loop = " LOOP" if _has_fuzzy_intra_loop(s.get("text") or "") else ""
        bad, reason = _detect_hallucination([s], audio_duration=None)
        flag = " [HALLUCINATED]" if bad else ""
        print(f"  {i+1}. [{s['start']:.1f}-{s['end']:.1f}s] "
              f"{(s.get('text') or '')[:80]!r}{flag}{loop}")

    print()
    hall, reason = _detect_hallucination(segments, user_dur)
    print(f"Global detector: hallucinated={hall} reason={reason}\n")

    if not hall:
        print("Detector says OK; recovery would not fire.")
        return 0

    print("=" * 60)
    print("Running _fill_gaps_with_reference...")
    print("=" * 60)
    merged = _fill_gaps_with_reference(segments, reference, user_dur)
    if merged is None:
        print("Gap-fill returned None")
        return 1
    print(f"\nMerged output: {len(merged)} segment(s)\n")
    for i, s in enumerate(merged[:35]):
        text = (s.get("text") or "")[:80]
        print(f"  {i+1:2d}. [{s['start']:6.1f}-{s['end']:6.1f}s] {text!r}")
    if len(merged) > 35:
        print(f"  ... ({len(merged) - 35} more)")

    # Sanity assertions
    print("\n=== Verdict ===")
    starts = [s["start"] for s in merged]
    if starts != sorted(starts):
        print("  ❌ NOT MONOTONIC")
        return 2
    print("  ✓ Monotonic by start time")
    if merged[-1]["end"] > user_dur + 0.1:
        print(f"  ❌ Last end ({merged[-1]['end']:.1f}) > audio_dur ({user_dur:.1f})")
        return 2
    print(f"  ✓ Last end ({merged[-1]['end']:.1f}) within audio_dur ({user_dur:.1f})")
    if len(merged) >= 12:
        print(f"  ✓ Output has {len(merged)} lines (>=12; user sees full coverage)")
    else:
        print(f"  ⚠️ Output has only {len(merged)} lines (<12; coverage may be sparse)")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
