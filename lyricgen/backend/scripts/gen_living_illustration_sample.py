"""Build the provider-free picker sample for the Living Illustration style.

The sample is intentionally code-authored: one basketball moves in a seamless
loop while the illustrated court, hoop, crowd and camera stay completely still.
It therefore demonstrates the product contract without spending a Veo request
or sneaking generic smoke/rain/particles into the card.

Run from lyricgen/backend:
  python3 scripts/gen_living_illustration_sample.py
"""
from __future__ import annotations

import math
import os
import subprocess

from PIL import Image, ImageDraw


WIDTH, HEIGHT = 720, 404
SCALE = 2
FPS = 24
DURATION = 5
OUT = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "..", "frontend", "public",
    "movement_samples", "animado.mp4",
))


def _xy(values):
    return tuple(int(value * SCALE) for value in values)


def _static_scene() -> Image.Image:
    image = Image.new("RGB", (WIDTH * SCALE, HEIGHT * SCALE), "#17162b")
    pixels = image.load()
    top = (30, 28, 67)
    bottom = (101, 35, 92)
    for y in range(HEIGHT * SCALE):
        p = y / (HEIGHT * SCALE - 1)
        color = tuple(int(a * (1 - p) + b * p) for a, b in zip(top, bottom))
        for x in range(WIDTH * SCALE):
            pixels[x, y] = color

    draw = ImageDraw.Draw(image)
    # Static graphic crowd silhouettes.
    for x in range(-18, WIDTH + 30, 34):
        h = 24 + (x * 13 % 34)
        draw.ellipse(_xy((x, 178 - h, x + 25, 203 - h)), fill="#242146")
        draw.rectangle(_xy((x - 5, 193 - h, x + 30, 235)), fill="#242146")

    # Court and fixed perspective lines.
    draw.polygon(
        [_xy((0, 232)), _xy((720, 208)), _xy((720, 404)), _xy((0, 404))],
        fill="#ef6c62",
    )
    for y, color in ((243, "#ff9b72"), (328, "#d64d61"), (389, "#b83b62")):
        draw.line(_xy((0, y, 720, y - 21)), fill=color, width=4 * SCALE)
    draw.line(_xy((360, 220, 360, 404)), fill="#f8c480", width=4 * SCALE)
    draw.ellipse(_xy((270, 258, 452, 391)), outline="#f8c480", width=4 * SCALE)

    # Static backboard, rim and net.
    draw.rectangle(_xy((552, 72, 565, 236)), fill="#282547")
    draw.rounded_rectangle(
        _xy((449, 64, 572, 139)), radius=8 * SCALE,
        fill="#f4e7d3", outline="#7de0d4", width=6 * SCALE,
    )
    draw.rectangle(_xy((489, 89, 540, 124)), outline="#ef6c62", width=4 * SCALE)
    draw.line(_xy((480, 142, 548, 142)), fill="#ffb15e", width=7 * SCALE)
    for x in range(484, 549, 13):
        draw.line(_xy((x, 145, 510 + (x - 516) * .34, 196)),
                  fill="#f4e7d3", width=2 * SCALE)
    draw.line(_xy((486, 158, 544, 158)), fill="#f4e7d3", width=2 * SCALE)
    draw.line(_xy((493, 176, 537, 176)), fill="#f4e7d3", width=2 * SCALE)

    # Static editorial accents: shapes, not atmospheric particles.
    draw.polygon(
        [_xy((36, 54)), _xy((78, 28)), _xy((105, 68)), _xy((64, 94))],
        fill="#7de0d4",
    )
    draw.line(_xy((104, 53, 215, 53)), fill="#f8c480", width=5 * SCALE)
    draw.line(_xy((104, 70, 179, 70)), fill="#f8c480", width=5 * SCALE)
    return image


def _frame(scene: Image.Image, frame_index: int) -> bytes:
    image = scene.copy()
    draw = ImageDraw.Draw(image)
    phase = frame_index / (FPS * DURATION)
    bounce_phase = (phase * 2.0) % 1.0
    x = 224 + 18 * math.sin(math.tau * phase)
    y = 319 - 126 * abs(math.sin(math.tau * bounce_phase))
    radius = 28

    # Shadow changes only as a consequence of the moving ball.
    height = max(0.0, (319 - y) / 126)
    shadow_w = 37 - 16 * height
    draw.ellipse(
        _xy((x - shadow_w, 340 - 7, x + shadow_w, 340 + 7)),
        fill=(115, 45, 77),
    )

    draw.ellipse(
        _xy((x - radius, y - radius, x + radius, y + radius)),
        fill="#ff9b3f", outline="#291d39", width=4 * SCALE,
    )
    angle = math.tau * phase * 2.0
    dx, dy = math.cos(angle) * radius, math.sin(angle) * radius
    draw.line(_xy((x - dx, y - dy, x + dx, y + dy)),
              fill="#7e3047", width=3 * SCALE)
    draw.arc(_xy((x - radius, y - radius * .48, x + radius, y + radius * .48)),
             0, 180, fill="#7e3047", width=3 * SCALE)
    draw.arc(_xy((x - radius * .48, y - radius, x + radius * .48, y + radius)),
             90, 270, fill="#7e3047", width=3 * SCALE)

    image = image.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)
    return image.tobytes()


def main() -> None:
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    command = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{WIDTH}x{HEIGHT}", "-r", str(FPS), "-i", "pipe:0",
        "-an", "-c:v", "libx264", "-preset", "slow", "-crf", "19",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", OUT,
    ]
    process = subprocess.Popen(command, stdin=subprocess.PIPE)
    assert process.stdin is not None
    scene = _static_scene()
    for frame_index in range(FPS * DURATION):
        process.stdin.write(_frame(scene, frame_index))
    process.stdin.close()
    if process.wait() != 0:
        raise SystemExit("ffmpeg failed while building Living Illustration sample")
    print(f"OK living illustration sample -> {OUT}")


if __name__ == "__main__":
    main()
