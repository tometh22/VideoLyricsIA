"""Option C — Baseline (current production): single Veo 8s + palindrome.

This is the control variant. Use exactly the production behavior:
- 1 Veo call (8s clip)
- Hand it to _get_background_clip_from_path which palindromes (16s cycle)
  and stream_loops to fill the audio duration
- Standard composition via generate_lyric_video

Tomas compares A/B/D against THIS to judge whether the changes are worth
the cost.

Cost: ~$0.80 (1 Veo 3.1-fast 8s call). Cached after first run via R2.

NOTE: We tried Veo durationSeconds=12 in a sanity check; Vertex Veo 3.1-fast
only supports durationSeconds=4|6|8 currently. So Option C stays at 8s —
this *is* the current production behavior, exactly. The naming "longer clip"
in the original plan turned out infeasible without switching to Veo 3 standard
(4× cost), which would defeat the comparison.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent.parent
sys.path.insert(0, str(BACKEND))

from common import (  # noqa: E402
    load_job, audio_duration, ensure_out_dir,
    compose_with_lyrics, trim_or_pad_to_duration,
)
from internal_tracking import ensure_internal_tracking_job  # noqa: E402


def build_baseline_background(audio_path: str, artist: str, song_title: str,
                              segments: list[dict], out_dir: Path,
                              benchmark_job_id: str) -> str:
    """Generate a Veo 8s clip + palindrome-loop it to the audio duration.

    Returns the path to the looped mp4 (duration ≈ audio duration).
    """
    from pipeline import (
        _generate_veo_video,
        _analyze_lyrics_for_background,
        _prerender_looped_bg,
    )

    dur = audio_duration(audio_path)
    veo_clip_path = str(out_dir / "c_veo_raw.mp4")
    looped_path = str(out_dir / "c_bg_palindrome.mp4")

    if Path(looped_path).exists() and Path(looped_path).stat().st_size > 100_000:
        print(f"  [opt-c] reusing cached palindrome bg: {Path(looped_path).name}")
        return looped_path

    # 1. Lyric analysis → prompt for Veo
    if Path(veo_clip_path).exists() and Path(veo_clip_path).stat().st_size > 100_000:
        print(f"  [opt-c] reusing cached Veo clip: {Path(veo_clip_path).name}")
    else:
        lyrics_text = "\n".join(s.get("text", "") for s in segments[:20])
        print(f"  [opt-c] analyzing lyrics for Veo prompt...")
        tracking_job_id = ensure_internal_tracking_job(
            f"loop-exp-c:{benchmark_job_id}", style="experiment")
        analysis = _analyze_lyrics_for_background(
            lyrics_text=lyrics_text,
            artist=artist,
            song_title=song_title,
            job_id=tracking_job_id,
            for_provider="veo",
            match_lyrics=True,
        )
        prompt = analysis.get("prompt") or f"{artist} {song_title} music video background, cinematic"
        print(f"  [opt-c] Veo prompt: {prompt[:200]}...")

        # 2. Veo call (existing helper, 8s default)
        print(f"  [opt-c] Veo 3.1-fast generating 8s clip (~$0.80, ~2 min)...")
        t0 = time.time()
        _generate_veo_video(
            prompt=prompt,
            output_path=veo_clip_path,
            job_id=tracking_job_id,
            cache_namespace=f"{artist}|{song_title}|opt-c",
            require_persistent_tracking=True,
        )
        print(f"  [opt-c] Veo done in {time.time() - t0:.0f}s")

    # 3. Palindrome loop to song duration (existing behavior)
    print(f"  [opt-c] palindrome loop to {dur:.0f}s...")
    _prerender_looped_bg(
        veo_clip_path, dur, str(out_dir),
        target_w=1920, target_h=1080,
        out_name=Path(looped_path).name,
    )
    return looped_path


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--job", default="8b9b19db22e9", help="job_id from benchmark dataset")
    args = p.parse_args()

    job = load_job(args.job)
    out_dir = ensure_out_dir(job["out_dir"])
    meta = job["metadata"]

    print(f"\n=== Option C (baseline: Veo 8s + palindrome) — job {args.job} ===")
    print(f"  Audio: {job['audio_path']}  ({audio_duration(job['audio_path']):.1f}s)")
    print(f"  Artist/Title: {meta.get('artist')} - {meta.get('song_title')}")

    bg_path = build_baseline_background(
        job["audio_path"], meta.get("artist", ""), meta.get("song_title", ""),
        job["segments"], out_dir, benchmark_job_id=job["job_id"],
    )

    out_path = compose_with_lyrics(
        audio_path=job["audio_path"],
        segments=job["segments"],
        bg_path=bg_path,
        out_dir=str(out_dir),
        artist=meta.get("artist", ""),
        song_title=meta.get("song_title", ""),
        out_name="option_c_baseline.mp4",
    )
    print(f"\n  ✓ Option C exported: {out_path}\n")


if __name__ == "__main__":
    main()
