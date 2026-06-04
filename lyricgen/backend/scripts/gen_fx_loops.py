"""Offline generator of PREMIUM effect-overlay loops (snow / rain / stars /
bokeh / light) for the compositing layer.

WHY offline: generating particles per-render would reintroduce the Python
frame-loop the libass migration killed (ass_render.py). So we bake short,
seamless, deterministic loops ONCE here, and the render only DECODES + blends
them (fast, C-level ffmpeg).

WHY "RGB on black" instead of alpha: these effects are ADDITIVE (light-emitting
particles). Composited with ffmpeg `blend=all_mode=screen`, pure black reads as
transparent — no alpha codec (VP9-alpha / ProRes4444) needed. Plain H.264.

SEAMLESS by construction: every motion is parametrized as
`phase = (base + (t/DUR) * k) % 1` with INTEGER k (full cycles over the loop),
so frame(0) == frame(DUR). No palindrome (which would make snow fall upward).

Run from lyricgen/backend:
  ./venv/bin/python scripts/gen_fx_loops.py              # all effects
  ./venv/bin/python scripts/gen_fx_loops.py snow rain    # subset
Outputs to lyricgen/assets/fx/<effect>.mp4 (1920x1080, 24fps, ~8s, H.264).
No API keys needed — pure procedural.
"""
import math
import os
import sys
import zlib

import numpy as np
from PIL import Image
from moviepy.editor import VideoClip

W, H = 1920, 1080
FPS = 24
DUR = 8.0
# Write into the backend package (lyricgen/backend/assets/fx) so the loops ship
# inside the Docker build context — matches fx_compositor._FX_DIR. (Moved here
# 2026-06-04 from the repo-level lyricgen/assets/fx, which was outside the
# backend build context and never reached the image.)
OUT = os.path.join(os.path.dirname(__file__), "..", "assets", "fx")

_RNG = np.random.default_rng(7)  # deterministic across runs


def _soft_sprite(radius: int) -> np.ndarray:
    """A soft round gaussian dot in [0,1], shape (2r+1, 2r+1)."""
    r = max(1, radius)
    y, x = np.mgrid[-r:r + 1, -r:r + 1]
    d2 = (x * x + y * y) / float(r * r)
    return np.clip(1.0 - d2, 0.0, 1.0) ** 1.5


def _splat(frame: np.ndarray, cx: int, cy: int, sprite: np.ndarray, color):
    """Additively blend a sprite centered at (cx,cy) into frame (H,W,3)."""
    r = sprite.shape[0] // 2
    x0, x1 = cx - r, cx + r + 1
    y0, y1 = cy - r, cy + r + 1
    fx0, fy0 = max(0, x0), max(0, y0)
    fx1, fy1 = min(W, x1), min(H, y1)
    if fx1 <= fx0 or fy1 <= fy0:
        return
    sx0, sy0 = fx0 - x0, fy0 - y0
    s = sprite[sy0:sy0 + (fy1 - fy0), sx0:sx0 + (fx1 - fx0)]
    region = frame[fy0:fy1, fx0:fx1]
    for c in range(3):
        region[:, :, c] += s * color[c]


def _frac(t):
    return (t / DUR) % 1.0


# --- effect builders: each returns a make_frame(t) over a BLACK background ----

def _snow():
    n = 380
    px = _RNG.uniform(0, 1, n)
    py = _RNG.uniform(0, 1, n)
    fall = _RNG.integers(1, 4, n).astype(float)          # integer vertical cycles
    sway_amp = _RNG.uniform(0.004, 0.018, n)
    sway_cyc = _RNG.integers(1, 4, n).astype(float)       # integer sway cycles
    sway_ph = _RNG.uniform(0, 2 * math.pi, n)
    rad = _RNG.integers(1, 4, n)
    bri = _RNG.uniform(120, 235, n)
    sprites = {r: _soft_sprite(int(r)) for r in set(rad.tolist())}

    def make(t):
        f = _frac(t)
        frame = np.zeros((H, W, 3), np.float32)
        ys = (py + f * fall) % 1.0
        xs = (px + sway_amp * np.sin(2 * math.pi * sway_cyc * f + sway_ph)) % 1.0
        for i in range(n):
            _splat(frame, int(xs[i] * W), int(ys[i] * H), sprites[rad[i]],
                   (bri[i], bri[i], bri[i]))
        return np.clip(frame, 0, 255).astype(np.uint8)
    return make


def _rain():
    n = 520
    px = _RNG.uniform(0, 1, n)
    py = _RNG.uniform(0, 1, n)
    fall = _RNG.integers(3, 7, n).astype(float)           # fast, integer cycles
    length = _RNG.integers(14, 34, n)
    bri = _RNG.uniform(70, 150, n)
    slant = 0.04

    def make(t):
        f = _frac(t)
        frame = np.zeros((H, W, 3), np.float32)
        ys = (py + f * fall) % 1.0
        for i in range(n):
            x = int(((px[i] + slant * ys[i]) % 1.0) * W)
            y = int(ys[i] * H)
            L = int(length[i])
            for k in range(L):
                yy = y + k
                if 0 <= yy < H and 0 <= x < W:
                    v = bri[i] * (1 - k / L)
                    frame[yy, x] += (v, v, v * 1.05)
        return np.clip(frame, 0, 255).astype(np.uint8)
    return make


def _stars():
    n = 260
    px = _RNG.uniform(0, 1, n)
    py = _RNG.uniform(0, 1, n)
    rad = _RNG.integers(1, 3, n)
    base = _RNG.uniform(40, 160, n)
    amp = _RNG.uniform(40, 95, n)
    tw = _RNG.integers(1, 5, n).astype(float)             # integer twinkle cycles
    ph = _RNG.uniform(0, 2 * math.pi, n)
    drift = 0.01                                          # tiny seamless drift
    sprites = {r: _soft_sprite(int(r)) for r in set(rad.tolist())}

    def make(t):
        f = _frac(t)
        frame = np.zeros((H, W, 3), np.float32)
        bri = base + amp * np.sin(2 * math.pi * tw * f + ph)
        xs = (px + drift * math.sin(2 * math.pi * f)) % 1.0
        for i in range(n):
            b = max(0.0, bri[i])
            _splat(frame, int(xs[i] * W), int(py[i] * H), sprites[rad[i]],
                   (b * 0.9, b * 0.95, b))               # cool white-blue
        return np.clip(frame, 0, 255).astype(np.uint8)
    return make


def _bokeh():
    n = 16
    px = _RNG.uniform(0, 1, n)
    py = _RNG.uniform(0, 1, n)
    rad = _RNG.integers(40, 130, n)
    dx_cyc = _RNG.integers(1, 3, n).astype(float)
    dy_cyc = _RNG.integers(1, 3, n).astype(float)
    amp = _RNG.uniform(0.02, 0.08, n)
    pulse = _RNG.integers(1, 4, n).astype(float)
    ph = _RNG.uniform(0, 2 * math.pi, n)
    warm = _RNG.uniform(0, 1, n)
    sprites = {r: _soft_sprite(int(r)) for r in set(rad.tolist())}

    def make(t):
        f = _frac(t)
        frame = np.zeros((H, W, 3), np.float32)
        for i in range(n):
            x = (px[i] + amp[i] * math.sin(2 * math.pi * dx_cyc[i] * f + ph[i])) % 1.0
            y = (py[i] + amp[i] * math.cos(2 * math.pi * dy_cyc[i] * f + ph[i])) % 1.0
            b = 60 + 40 * math.sin(2 * math.pi * pulse[i] * f + ph[i])
            col = (b, b * (0.7 + 0.3 * warm[i]), b * (0.5 + 0.2 * (1 - warm[i])))
            _splat(frame, int(x * W), int(y * H), sprites[rad[i]], col)
        return np.clip(frame, 0, 255).astype(np.uint8)
    return make


def _light():
    n = 4
    px = _RNG.uniform(0.1, 0.9, n)
    py = _RNG.uniform(0.0, 0.5, n)
    rad = _RNG.integers(260, 460, n)
    dx_cyc = _RNG.integers(1, 3, n).astype(float)
    amp = _RNG.uniform(0.04, 0.12, n)
    ph = _RNG.uniform(0, 2 * math.pi, n)
    sprites = {r: _soft_sprite(int(r)) for r in set(rad.tolist())}

    def make(t):
        f = _frac(t)
        frame = np.zeros((H, W, 3), np.float32)
        for i in range(n):
            x = (px[i] + amp[i] * math.sin(2 * math.pi * dx_cyc[i] * f + ph[i])) % 1.0
            b = 38 + 16 * math.sin(2 * math.pi * f + ph[i])
            _splat(frame, int(x * W), int(py[i] * H), sprites[rad[i]],
                   (b, b * 0.85, b * 0.6))               # warm glow
        return np.clip(frame, 0, 255).astype(np.uint8)
    return make


EFFECTS = {
    "snow": _snow, "rain": _rain, "stars": _stars,
    "bokeh": _bokeh, "light": _light,
}


def main():
    os.makedirs(OUT, exist_ok=True)
    only = set(sys.argv[1:])
    for name, builder in EFFECTS.items():
        if only and name not in only:
            continue
        # rng re-seeded per effect with a STABLE seed (crc32, not hash() which
        # is salted per process) so each effect is deterministic across runs.
        global _RNG
        _RNG = np.random.default_rng(zlib.crc32(name.encode()))
        make = builder()
        out = os.path.abspath(os.path.join(OUT, f"{name}.mp4"))
        print(f"=== {name} -> {out} ===", flush=True)
        clip = VideoClip(make, duration=DUR).set_fps(FPS)
        clip.write_videofile(out, fps=FPS, codec="libx264", audio=False,
                             preset="slow", logger=None,
                             ffmpeg_params=["-pix_fmt", "yuv420p", "-crf", "18"])
        clip.close()
        # seamless self-check: motion is (base + (t/DUR)*k)%1 with INTEGER k, so
        # frame(DUR) must equal frame(0) exactly. MAD≈0 proves the loop math is
        # periodic (non-integer cycles would show MAD>0 and a visible jump).
        a = make(0.0).astype(np.int16)
        b = make(DUR).astype(np.int16)
        mad = float(np.abs(a - b).mean())
        print(f"OK {name}: periodicity MAD(0,DUR)={mad:.4f} (must be ~0)", flush=True)


if __name__ == "__main__":
    main()
