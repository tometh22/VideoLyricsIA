"""Build lightweight browser previews and picker posters from backend FX loops.

Run from lyricgen/backend:
  python3 scripts/gen_fx_previews.py
  python3 scripts/gen_fx_previews.py liquid_glass beat_flash

The backend loop remains the render source of truth. This script creates:
  frontend/public/fx_raw/<effect>.mp4     live-composer layer (854x480)
  frontend/public/fx_samples/<effect>.mp4 picker demo over a neutral base
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

    mode = fx.effect_blend(name)
    opacity = fx.effect_opacity(name)
    # Multiply needs a mid-light canvas to demonstrate its dark motion in a
    # tiny card; screen effects need the inverse so emitted light reads.
    sample_bg = "0x7e86a3" if mode == "multiply" else "0x120d2d"
    _run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c={sample_bg}:s=480x270:r=20:d=5",
        "-i", source,
        "-filter_complex",
        f"[0:v]format=gbrp[base];"
        f"[1:v]scale=480:270:flags=lanczos,format=gbrp[layer];"
        f"[base][layer]blend=all_mode={mode}:all_opacity={opacity:.2f}:shortest=1,"
        "format=yuv420p[out]",
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
