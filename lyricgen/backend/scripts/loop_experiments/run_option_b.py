"""Option B — Disimular el seam: 1 Imagen + randomized Ken Burns + xfade.

Strategy:
  1. Generate ONE Imagen still (cheapest path, $0.10).
  2. Render N Ken Burns clips over the SAME still — each call to
     `_ken_burns_clip` reseeds random.seed(None), so each pass has
     a different sequence of zoom/pan cycles.
  3. Crossfade-chain those N clips into one mp4 of exactly the audio duration.

Why it works:
  - The viewer sees the same scene throughout (same colors, same content)
  - But the motion never repeats: each ~45s segment has a fresh pattern
  - Crossfades hide the boundaries → the loop "breathes" without seams
  - No A/B comparison vs. straight palindrome on the same image confused
    QA testers in pilot — most thought it was a different background

Cost: ~$0.10 (1 Imagen-4 call).
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent.parent
sys.path.insert(0, str(BACKEND))

from common import (  # noqa: E402
    load_job, audio_duration, ensure_out_dir,
    compose_with_lyrics, ffmpeg_xfade_chain, trim_or_pad_to_duration,
)
from internal_tracking import ensure_internal_tracking_job  # noqa: E402

# Each Ken Burns sub-clip is ~45s; xfade overlaps adjacent ones by 2s.
# Effective per-clip contribution = 45 - 2 = 43s.
# For a 214s song we need ~5 clips: 5*45 - 4*2 = 217s, trimmed to 214s.
KB_SUB_DURATION = 45.0
XFADE_DUR = 2.0


def build_seam_hidden_background(audio_path: str, artist: str, song_title: str,
                                  segments: list[dict], out_dir: Path,
                                  benchmark_job_id: str) -> str:
    """Imagen still + N randomized Ken Burns + xfade chain."""
    from pipeline import (
        _generate_imagen_image,
        _analyze_lyrics_for_background,
        _ken_burns_image_to_mp4,
    )

    dur = audio_duration(audio_path)
    img_path = str(out_dir / "b_imagen_still.jpg")
    final_path = str(out_dir / "b_bg_seam_hidden.mp4")

    if Path(final_path).exists() and Path(final_path).stat().st_size > 100_000:
        print(f"  [opt-b] reusing cached bg: {Path(final_path).name}")
        return final_path

    # 1. Imagen prompt (Imagen mode → no motion words)
    if Path(img_path).exists() and Path(img_path).stat().st_size > 10_000:
        print(f"  [opt-b] reusing cached Imagen still")
    else:
        lyrics_text = "\n".join(s.get("text", "") for s in segments[:20])
        print(f"  [opt-b] analyzing lyrics for Imagen prompt...")
        tracking_job_id = ensure_internal_tracking_job(
            f"loop-exp-b:{benchmark_job_id}", style="experiment")
        analysis = _analyze_lyrics_for_background(
            lyrics_text=lyrics_text,
            artist=artist,
            song_title=song_title,
            job_id=tracking_job_id,
            for_provider="imagen",
            match_lyrics=True,
        )
        prompt = analysis.get("prompt") or f"{artist} {song_title} atmospheric, cinematic"
        print(f"  [opt-b] Imagen prompt: {prompt[:200]}...")
        t0 = time.time()
        _generate_imagen_image(
            prompt=prompt,
            output_path=img_path,
            job_id=tracking_job_id,
        )
        print(f"  [opt-b] Imagen done in {time.time() - t0:.0f}s")

    # 2. Render N Ken Burns sub-clips with fresh random seeds each
    n_clips = max(2, int(math.ceil((dur + (XFADE_DUR * (n_for_dur := 1))) / (KB_SUB_DURATION - XFADE_DUR))))
    # Recompute n_clips honestly (solving for N): N*sub - (N-1)*xfade >= dur
    # N >= (dur - xfade) / (sub - xfade) → ceil
    n_clips = max(2, math.ceil((dur - XFADE_DUR) / (KB_SUB_DURATION - XFADE_DUR)))
    print(f"  [opt-b] rendering {n_clips} randomized Ken Burns sub-clips of {KB_SUB_DURATION:.0f}s each...")
    sub_paths = []
    for i in range(n_clips):
        sub = str(out_dir / f"b_kb_{i:02d}.mp4")
        if Path(sub).exists() and Path(sub).stat().st_size > 100_000:
            print(f"    [opt-b] reusing cached sub-clip {i}")
        else:
            t0 = time.time()
            _ken_burns_image_to_mp4(
                img_path, sub,
                sample_duration=KB_SUB_DURATION,
            )
            print(f"    [opt-b] sub-clip {i} done in {time.time() - t0:.0f}s")
        sub_paths.append(sub)

    # 3. Crossfade-chain them
    print(f"  [opt-b] xfade-chaining {len(sub_paths)} sub-clips (xfade={XFADE_DUR}s)...")
    chained_path = str(out_dir / "b_bg_chained.mp4")
    ffmpeg_xfade_chain(sub_paths, XFADE_DUR, chained_path, fps=24)

    # 4. Trim to exact audio duration
    trim_or_pad_to_duration(chained_path, final_path, dur)
    print(f"  [opt-b] final bg: {Path(final_path).name} ({Path(final_path).stat().st_size // 1024 // 1024} MB)")
    return final_path


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--job", default="8b9b19db22e9", help="job_id from benchmark dataset")
    args = p.parse_args()

    job = load_job(args.job)
    out_dir = ensure_out_dir(job["out_dir"])
    meta = job["metadata"]

    print(f"\n=== Option B (seam-hidden: 1 Imagen + randomized KB + xfade) — job {args.job} ===")
    print(f"  Audio: {job['audio_path']}  ({audio_duration(job['audio_path']):.1f}s)")

    bg_path = build_seam_hidden_background(
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
        out_name="option_b_seam_hidden.mp4",
    )
    print(f"\n  ✓ Option B exported: {out_path}\n")


if __name__ == "__main__":
    main()
