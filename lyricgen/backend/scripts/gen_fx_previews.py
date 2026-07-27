"""Build lightweight browser previews and picker posters from backend FX loops.

Run from lyricgen/backend:
  python3 scripts/gen_fx_previews.py
  python3 scripts/gen_fx_previews.py liquid_glass beat_flash

The backend loop remains the render source of truth. This script creates:
  frontend/public/fx_raw/<effect>.mp4     live-composer layer (854x480)
  frontend/public/fx_samples/<effect>.mp4 picker demo over a real photo,
                                                   through the production graph
  frontend/public/fx_samples/<effect>.jpg picker poster
"""
from __future__ import annotations

import os
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.dirname(SCRIPT_DIR)
REPO_DIR = os.path.dirname(BACKEND_DIR)
FRONTEND_PUBLIC = os.path.join(REPO_DIR, "frontend", "public")

sys.path.insert(0, BACKEND_DIR)
import fx_compositor as fx  # noqa: E402


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def build(name: str) -> None:
    source = fx.effect_path(name)
    if not source:
        raise RuntimeError(f"missing backend loop for {name}")

    raw_dir = os.path.join(FRONTEND_PUBLIC, "fx_raw")
    sample_dir = os.path.join(FRONTEND_PUBLIC, "fx_samples")
    os.makedirs(raw_dir, exist_ok=True)
    os.makedirs(sample_dir, exist_ok=True)
    raw = os.path.join(raw_dir, f"{name}.mp4")
    sample = os.path.join(sample_dir, f"{name}.mp4")
    poster = os.path.join(sample_dir, f"{name}.jpg")

    common = [
        "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "25",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
    ]
    raw_crf = "30" if name == "halftone" else "25"
    _run([
        "ffmpeg", "-y", "-loglevel", "error", "-i", source,
        "-vf", "scale=854:480:flags=lanczos,fps=20", "-t", "8",
        "-an", "-c:v", "libx264", "-preset", "fast", "-crf", raw_crf,
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", raw,
    ])

    # Picker previews now exercise the exact production compositor over a
    # representative fixed photo.  A neutral color previously made geometric
    # transforms look like overlays (or completely invisible), and could not
    # prove that the selected photo's own pixels were being transformed.
    # Clean fixed-photo crop (no lyric text baked in). The production graph
    # scales-to-fill before cropping, so this wide source becomes a natural
    # 16:9 coral scene without distortion.
    photo = os.path.join(
        FRONTEND_PUBLIC, "movement_samples", "foto-fija.jpg"
    )
    rhythm = (
        fx.EffectRhythm(
            120.0,
            tuple(i * 0.5 + 0.08 for i in range(10)),
            tuple(1.0 if i % 2 == 0 else 0.58 for i in range(10)),
        )
        if fx.is_reactive_effect(name)
        else None
    )
    graph, use_complex, extra = fx.build_video_filter(
        ass_basename=None,
        font_dir="",
        width=480,
        height=270,
        effect=name,
        rhythm=rhythm,
    )
    if not use_complex:
        raise RuntimeError(f"effect graph unexpectedly simple for {name}")
    _run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-loop", "1", "-framerate", "20", "-i", photo,
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
        *extra,
        "-filter_complex", graph,
        "-map", "[out]", "-t", "5", *common, sample,
    ])

    # Reactive loops peak at the start of each half-second beat. A 50 ms frame
    # makes the poster representative instead of capturing the dark decay.
    poster_at = "0.05" if fx.is_reactive_effect(name) else "1.35"
    _run([
        "ffmpeg", "-y", "-loglevel", "error", "-ss", poster_at,
        "-i", sample, "-frames:v", "1", "-q:v", "2", poster,
    ])
    print(f"OK preview {name}")


def main() -> None:
    requested = sys.argv[1:]
    names = requested or list(fx.EFFECTS)
    unknown = sorted(set(names) - set(fx.EFFECTS))
    if unknown:
        raise SystemExit(f"unknown effects: {', '.join(unknown)}")
    for name in names:
        build(name)


if __name__ == "__main__":
    main()
