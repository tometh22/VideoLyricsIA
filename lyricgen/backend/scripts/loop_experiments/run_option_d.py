"""Option D — Híbrido chorus/verse: Veo en chorus + Imagen/KB en verses.

Strategy:
  1. Detect the chorus: lines whose normalized text repeats ≥2 times in the
     song. The LONGEST contiguous run of chorus segments = the "chorus region".
  2. 1 Veo call → palindrome to chorus_dur seconds → that's our chorus bg
  3. 1 Imagen + Ken Burns → covers verses (everything outside the chorus)
  4. Stitch: pre-verse + chorus + post-verse with crossfade at boundaries

Why it works:
  - The chorus is what the viewer remembers most → fresh Veo content there
  - Verses (which the brain attends to less) get cheaper Imagen Ken Burns
  - Sharp content change at chorus boundary doubles as a "drop" cue,
    reinforcing song structure visually

Cost: ~$0.90 (1 Veo 8s ≈ $0.80 + 1 Imagen ≈ $0.10).

If the song has no detectable chorus (e.g. single-verse poem, fully through-
composed), fall back to Option B behavior with a note.
"""
from __future__ import annotations

import argparse
import math
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent.parent
sys.path.insert(0, str(BACKEND))

from common import (  # noqa: E402
    load_job, audio_duration, ensure_out_dir,
    compose_with_lyrics, ffmpeg_xfade_chain, trim_or_pad_to_duration,
)
from internal_tracking import ensure_internal_tracking_job  # noqa: E402

XFADE_DUR = 1.5


def _norm(t: str) -> str:
    return re.sub(r"[^a-záéíóúñü ]", "", (t or "").lower()).strip()


def detect_chorus_region(segments: list[dict]) -> tuple[int, int] | None:
    """Return (start_seg_idx, end_seg_idx_inclusive) of the LONGEST contiguous
    run of repeated-text lines. None if no useful chorus detected.

    A line is "chorus-like" if its normalized text appears ≥2 times.
    """
    if not segments:
        return None
    counts = Counter(_norm(s.get("text", "")) for s in segments)
    is_chorus = [counts[_norm(s.get("text", ""))] >= 2 for s in segments]

    # Find longest contiguous run of True
    best = None
    best_len = 0
    i = 0
    while i < len(is_chorus):
        if is_chorus[i]:
            j = i
            while j < len(is_chorus) and is_chorus[j]:
                j += 1
            run_len = j - i
            if run_len > best_len:
                best_len = run_len
                best = (i, j - 1)
            i = j
        else:
            i += 1

    if best_len < 3:
        # Too short to be a meaningful chorus
        return None
    return best


def build_hybrid_background(audio_path: str, artist: str, song_title: str,
                             segments: list[dict], out_dir: Path,
                             benchmark_job_id: str) -> str:
    """Veo for chorus region, Imagen+KB for verses, xfade at boundaries."""
    from pipeline import (
        _generate_veo_video,
        _generate_imagen_image,
        _analyze_lyrics_for_background,
        _ken_burns_image_to_mp4,
        _prerender_looped_bg,
    )

    dur = audio_duration(audio_path)
    final_path = str(out_dir / "d_bg_hybrid.mp4")

    if Path(final_path).exists() and Path(final_path).stat().st_size > 100_000:
        print(f"  [opt-d] reusing cached bg: {Path(final_path).name}")
        return final_path

    # 1. Detect chorus region
    chorus = detect_chorus_region(segments)
    if chorus is None:
        print(f"  [opt-d] WARN: no detectable chorus, falling back to verse-only (Imagen + KB whole song)")
        chorus_start_t = chorus_end_t = None
    else:
        i_start, i_end = chorus
        chorus_start_t = segments[i_start]["start"]
        chorus_end_t = segments[i_end]["end"]
        print(f"  [opt-d] chorus region: segs[{i_start}..{i_end}] = {chorus_start_t:.1f}s..{chorus_end_t:.1f}s "
              f"({chorus_end_t - chorus_start_t:.1f}s)")

    # 2. Imagen for verses (cover full song; we'll crop to verse regions later)
    verse_text = "\n".join(
        s.get("text", "")
        for i, s in enumerate(segments)
        if not (chorus and chorus[0] <= i <= chorus[1])
    )
    img_path = str(out_dir / "d_imagen_verse.jpg")
    if not (Path(img_path).exists() and Path(img_path).stat().st_size > 10_000):
        print(f"  [opt-d] analyzing verse lyrics for Imagen prompt...")
        verse_tracking_job_id = ensure_internal_tracking_job(
            f"loop-exp-d:{benchmark_job_id}:verse", style="experiment")
        analysis = _analyze_lyrics_for_background(
            lyrics_text=verse_text or "atmospheric introspective",
            artist=artist,
            song_title=song_title,
            job_id=verse_tracking_job_id,
            for_provider="imagen",
            match_lyrics=True,
        )
        prompt = analysis.get("prompt") or f"{artist} {song_title} verse, intimate"
        print(f"  [opt-d] verse Imagen prompt: {prompt[:160]}...")
        t0 = time.time()
        _generate_imagen_image(
            prompt=prompt, output_path=img_path,
            job_id=verse_tracking_job_id)
        print(f"  [opt-d] Imagen done in {time.time() - t0:.0f}s")
    else:
        print(f"  [opt-d] reusing cached verse Imagen still")

    # Ken Burns full-song length (we'll splice in chorus later)
    kb_full_path = str(out_dir / "d_kb_full.mp4")
    if not (Path(kb_full_path).exists() and Path(kb_full_path).stat().st_size > 100_000):
        print(f"  [opt-d] rendering Ken Burns over full {dur:.0f}s...")
        _ken_burns_image_to_mp4(img_path, kb_full_path, sample_duration=dur + 4.0)
        # Trim to exact duration (KB can run slightly over)
    else:
        print(f"  [opt-d] reusing cached KB-full")

    if chorus is None:
        # No chorus → final bg IS the Ken Burns
        trim_or_pad_to_duration(kb_full_path, final_path, dur)
        return final_path

    # 3. Veo for chorus
    veo_path = str(out_dir / "d_veo_raw.mp4")
    veo_looped_path = str(out_dir / "d_veo_chorus.mp4")
    chorus_dur = chorus_end_t - chorus_start_t

    if not (Path(veo_path).exists() and Path(veo_path).stat().st_size > 100_000):
        chorus_text = "\n".join(
            segments[i].get("text", "") for i in range(chorus[0], chorus[1] + 1)
        )
        print(f"  [opt-d] analyzing chorus lyrics for Veo prompt...")
        chorus_tracking_job_id = ensure_internal_tracking_job(
            f"loop-exp-d:{benchmark_job_id}:chorus", style="experiment")
        analysis = _analyze_lyrics_for_background(
            lyrics_text=chorus_text,
            artist=artist,
            song_title=song_title,
            job_id=chorus_tracking_job_id,
            for_provider="veo",
            match_lyrics=True,
        )
        prompt = analysis.get("prompt") or f"{artist} {song_title} chorus, energetic"
        print(f"  [opt-d] chorus Veo prompt: {prompt[:160]}...")
        t0 = time.time()
        _generate_veo_video(
            prompt=prompt,
            output_path=veo_path,
            job_id=chorus_tracking_job_id,
            cache_namespace=f"{artist}|{song_title}|opt-d-chorus",
            require_persistent_tracking=True,
        )
        print(f"  [opt-d] Veo done in {time.time() - t0:.0f}s")
    else:
        print(f"  [opt-d] reusing cached Veo chorus clip")

    if not (Path(veo_looped_path).exists() and Path(veo_looped_path).stat().st_size > 100_000):
        print(f"  [opt-d] palindrome-looping Veo to chorus duration {chorus_dur:.1f}s...")
        _prerender_looped_bg(
            veo_path, chorus_dur, str(out_dir),
            target_w=1920, target_h=1080,
            out_name=Path(veo_looped_path).name,
        )

    # 4. Split KB into pre-chorus and post-chorus segments
    kb_pre_path = str(out_dir / "d_kb_pre.mp4")
    kb_post_path = str(out_dir / "d_kb_post.mp4")

    import subprocess as sp
    # Pre: 0..chorus_start + XFADE_DUR (so the xfade can overlap)
    pre_dur = chorus_start_t + XFADE_DUR
    if pre_dur > 0.5:
        sp.run(
            ["ffmpeg", "-y", "-i", kb_full_path, "-t", f"{pre_dur:.3f}",
             "-c:v", "libx264", "-preset", "fast", "-crf", "20",
             "-pix_fmt", "yuv420p", "-an", "-r", "24",
             kb_pre_path],
            check=True, capture_output=True,
        )
    else:
        kb_pre_path = None

    # Post: chorus_end - XFADE_DUR .. end
    post_start = max(0.0, chorus_end_t - XFADE_DUR)
    post_dur = dur - post_start
    if post_dur > 0.5:
        sp.run(
            ["ffmpeg", "-y", "-ss", f"{post_start:.3f}", "-i", kb_full_path,
             "-t", f"{post_dur:.3f}",
             "-c:v", "libx264", "-preset", "fast", "-crf", "20",
             "-pix_fmt", "yuv420p", "-an", "-r", "24",
             kb_post_path],
            check=True, capture_output=True,
        )
    else:
        kb_post_path = None

    # 5. xfade chain: KB-pre → Veo-chorus → KB-post
    chain_inputs = [p for p in (kb_pre_path, veo_looped_path, kb_post_path) if p]
    print(f"  [opt-d] xfade-chaining {len(chain_inputs)} segments...")
    chained_path = str(out_dir / "d_bg_chained.mp4")
    ffmpeg_xfade_chain(chain_inputs, XFADE_DUR, chained_path, fps=24)

    # 6. Trim to exact audio duration
    trim_or_pad_to_duration(chained_path, final_path, dur)
    print(f"  [opt-d] final bg: {Path(final_path).name} ({Path(final_path).stat().st_size // 1024 // 1024} MB)")
    return final_path


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--job", default="8b9b19db22e9", help="job_id from benchmark dataset")
    args = p.parse_args()

    job = load_job(args.job)
    out_dir = ensure_out_dir(job["out_dir"])
    meta = job["metadata"]

    print(f"\n=== Option D (hybrid chorus/verse: Veo + Imagen+KB) — job {args.job} ===")
    print(f"  Audio: {job['audio_path']}  ({audio_duration(job['audio_path']):.1f}s)")

    bg_path = build_hybrid_background(
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
        out_name="option_d_hybrid.mp4",
    )
    print(f"\n  ✓ Option D exported: {out_path}\n")


if __name__ == "__main__":
    main()
