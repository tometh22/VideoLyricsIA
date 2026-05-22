"""Option A — Variedad genuina: 4 Imagen stills + Ken Burns + xfade.

Strategy:
  1. Analyze lyrics 4× with different sub-prompt hints (verse 1, verse 2,
     chorus, outro) to get 4 distinct visual scenes that still share the
     song's overall mood.
  2. Generate one Imagen still per scene (4 total).
  3. Render Ken Burns over each.
  4. Crossfade-chain the 4 clips into one mp4 of exactly the audio duration.

Why it works:
  - Viewer sees genuinely new content every ~55s
  - The Imagen prompts come from the song's own lyric arc, so the scenes
    feel cohesive with the music, not random
  - Zero "I've seen this motion before" — every cycle is a different image
  - Crossfades make transitions feel cinematic, not abrupt

Cost: ~$0.40 (4 × Imagen-4 calls).
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

N_SCENES = 4
KB_SUB_DURATION = 60.0  # Each scene shows for ~60s before crossfade
XFADE_DUR = 2.0


def _split_lyrics_into_acts(segments: list[dict], n_acts: int) -> list[str]:
    """Split segment text into n_acts roughly-equal sections.
    Each section becomes the lyrics context for a separate Imagen call.

    For a 3-4 min song with 30+ segments, this gives Gemini ~8 lines per
    act → enough lyrical context to pick a coherent scene, distinct from
    the other acts.
    """
    if not segments:
        return [""] * n_acts
    chunk_size = max(1, len(segments) // n_acts)
    acts = []
    for i in range(n_acts):
        start = i * chunk_size
        end = (i + 1) * chunk_size if i < n_acts - 1 else len(segments)
        chunk = segments[start:end]
        acts.append("\n".join(s.get("text", "") for s in chunk))
    return acts


def build_variety_background(audio_path: str, artist: str, song_title: str,
                              segments: list[dict], out_dir: Path) -> str:
    """4 distinct Imagen stills, each with own Ken Burns, xfade-chained."""
    from pipeline import (
        _generate_imagen_image,
        _analyze_lyrics_for_background,
        _ken_burns_image_to_mp4,
    )

    dur = audio_duration(audio_path)
    final_path = str(out_dir / "a_bg_variety.mp4")

    if Path(final_path).exists() and Path(final_path).stat().st_size > 100_000:
        print(f"  [opt-a] reusing cached bg: {Path(final_path).name}")
        return final_path

    # 1. Split lyrics into 4 acts → 4 distinct prompts
    acts = _split_lyrics_into_acts(segments, N_SCENES)

    img_paths = []
    for i, act_text in enumerate(acts):
        img_path = str(out_dir / f"a_imagen_{i:02d}.jpg")
        if Path(img_path).exists() and Path(img_path).stat().st_size > 10_000:
            print(f"  [opt-a] reusing cached Imagen still {i}")
            img_paths.append(img_path)
            continue

        print(f"  [opt-a] analyzing act {i+1}/{N_SCENES} for Imagen prompt...")
        analysis = _analyze_lyrics_for_background(
            lyrics_text=act_text,
            artist=artist,
            song_title=song_title,
            job_id=f"loop_exp_a_act{i}",
            for_provider="imagen",
            match_lyrics=True,
        )
        prompt = analysis.get("prompt") or f"{artist} {song_title} act {i+1}, cinematic"
        print(f"  [opt-a] act {i+1} prompt: {prompt[:160]}...")

        t0 = time.time()
        _generate_imagen_image(
            prompt=prompt,
            output_path=img_path,
            job_id=f"loop_exp_a_act{i}",
        )
        print(f"  [opt-a] Imagen act {i+1} done in {time.time() - t0:.0f}s")
        img_paths.append(img_path)

    # 2. Render Ken Burns over each still
    # Recompute KB sub-duration so 4 clips with xfade cover the song.
    # N*sub - (N-1)*xfade >= dur → sub >= (dur + (N-1)*xfade) / N
    needed_sub = (dur + (N_SCENES - 1) * XFADE_DUR) / N_SCENES
    sub_dur = max(KB_SUB_DURATION, needed_sub + 1.0)  # +1s buffer for trim
    print(f"  [opt-a] rendering {N_SCENES} Ken Burns clips of {sub_dur:.0f}s each...")

    kb_paths = []
    for i, img_path in enumerate(img_paths):
        kb_path = str(out_dir / f"a_kb_{i:02d}.mp4")
        if Path(kb_path).exists() and Path(kb_path).stat().st_size > 100_000:
            print(f"    [opt-a] reusing cached KB clip {i}")
        else:
            t0 = time.time()
            from pipeline import _ken_burns_image_to_mp4
            _ken_burns_image_to_mp4(img_path, kb_path, sample_duration=sub_dur)
            print(f"    [opt-a] KB clip {i} done in {time.time() - t0:.0f}s")
        kb_paths.append(kb_path)

    # 3. Crossfade-chain
    print(f"  [opt-a] xfade-chaining {len(kb_paths)} scene clips (xfade={XFADE_DUR}s)...")
    chained_path = str(out_dir / "a_bg_chained.mp4")
    ffmpeg_xfade_chain(kb_paths, XFADE_DUR, chained_path, fps=24)

    # 4. Trim to exact audio duration
    trim_or_pad_to_duration(chained_path, final_path, dur)
    print(f"  [opt-a] final bg: {Path(final_path).name} ({Path(final_path).stat().st_size // 1024 // 1024} MB)")
    return final_path


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--job", default="8b9b19db22e9", help="job_id from benchmark dataset")
    args = p.parse_args()

    job = load_job(args.job)
    out_dir = ensure_out_dir(job["out_dir"])
    meta = job["metadata"]

    print(f"\n=== Option A (variety: 4 Imagen + Ken Burns + xfade) — job {args.job} ===")
    print(f"  Audio: {job['audio_path']}  ({audio_duration(job['audio_path']):.1f}s)")

    bg_path = build_variety_background(
        job["audio_path"], meta.get("artist", ""), meta.get("song_title", ""),
        job["segments"], out_dir,
    )

    out_path = compose_with_lyrics(
        audio_path=job["audio_path"],
        segments=job["segments"],
        bg_path=bg_path,
        out_dir=str(out_dir),
        artist=meta.get("artist", ""),
        song_title=meta.get("song_title", ""),
        out_name="option_a_variety.mp4",
    )
    print(f"\n  ✓ Option A exported: {out_path}\n")


if __name__ == "__main__":
    main()
