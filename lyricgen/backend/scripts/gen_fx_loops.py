"""Offline generator of PREMIUM effect-overlay loops for the compositing layer.

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
from PIL import Image, ImageDraw

try:
    # Production pins MoviePy 1.x.
    from moviepy.editor import VideoClip
    _MOVIEPY_V2 = False
except ModuleNotFoundError:  # Local tooling may already have MoviePy 2.x.
    from moviepy import VideoClip
    _MOVIEPY_V2 = True

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


def _soft_ellipse(rx: int, ry: int, angle: float) -> np.ndarray:
    """Soft rotated ellipse used for petals and confetti-like shapes."""
    r = max(rx, ry)
    y, x = np.mgrid[-r:r + 1, -r:r + 1]
    ca, sa = math.cos(angle), math.sin(angle)
    xr = x * ca + y * sa
    yr = -x * sa + y * ca
    d2 = (xr / max(1, rx)) ** 2 + (yr / max(1, ry)) ** 2
    return np.clip(1.0 - d2, 0.0, 1.0) ** 1.2


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
    """Large warm/cool glows that travel through the whole frame.

    The previous loop assigned each glow a fixed ``py`` and only oscillated
    ``x`` by 4–12% of the frame. On a still photo that read as four lamps
    nailed to the same spots for the whole song. These Lissajous paths move in
    BOTH axes and cover most of the canvas while remaining perfectly periodic.
    """
    n = 4
    rad = _RNG.integers(240, 430, n)
    dx_cyc = _RNG.integers(1, 3, n).astype(float)
    dy_cyc = _RNG.integers(1, 3, n).astype(float)
    amp_x = _RNG.uniform(0.30, 0.48, n)
    amp_y = _RNG.uniform(0.22, 0.42, n)
    ph = _RNG.uniform(0, 2 * math.pi, n)
    sprites = {r: _soft_sprite(int(r)) for r in set(rad.tolist())}

    def make(t):
        f = _frac(t)
        frame = np.zeros((H, W, 3), np.float32)
        for i in range(n):
            x = 0.5 + amp_x[i] * math.sin(
                2 * math.pi * dx_cyc[i] * f + ph[i]
            )
            y = 0.5 + amp_y[i] * math.cos(
                2 * math.pi * dy_cyc[i] * f + ph[i] * 0.71
            )
            b = 34 + 20 * math.sin(2 * math.pi * f + ph[i])
            # Alternating warm/cool glows feel like moving light leaks instead
            # of one uniform yellow wash.
            col = ((b, b * 0.82, b * 0.52) if i % 2 == 0
                   else (b * 0.48, b * 0.68, b))
            _splat(frame, int(x * W), int(y * H), sprites[rad[i]], col)
        return np.clip(frame, 0, 255).astype(np.uint8)
    return make


def _dust():
    """Warm dust motes drifting slowly in depth."""
    n = 230
    px = _RNG.uniform(0, 1, n)
    py = _RNG.uniform(0, 1, n)
    rad = _RNG.integers(1, 6, n)
    drift_cyc = _RNG.integers(1, 4, n).astype(float)
    sway_cyc = _RNG.integers(1, 3, n).astype(float)
    amp_x = _RNG.uniform(0.004, 0.025, n)
    amp_y = _RNG.uniform(0.005, 0.035, n)
    ph = _RNG.uniform(0, 2 * math.pi, n)
    bri = _RNG.uniform(45, 135, n)
    sprites = {r: _soft_sprite(int(r)) for r in set(rad.tolist())}

    def make(t):
        f = _frac(t)
        frame = np.zeros((H, W, 3), np.float32)
        for i in range(n):
            x = (px[i] + amp_x[i] * math.sin(
                2 * math.pi * sway_cyc[i] * f + ph[i]
            )) % 1.0
            y = (py[i] + amp_y[i] * math.cos(
                2 * math.pi * drift_cyc[i] * f + ph[i]
            )) % 1.0
            pulse = 0.45 + 0.55 * math.sin(
                2 * math.pi * drift_cyc[i] * f + ph[i]
            ) ** 2
            b = bri[i] * pulse
            _splat(frame, int(x * W), int(y * H), sprites[rad[i]],
                   (b, b * 0.78, b * 0.48))
        return np.clip(frame, 0, 255).astype(np.uint8)
    return make


def _embers():
    """Orange sparks rising with a gentle sideways curl."""
    n = 180
    px = _RNG.uniform(0, 1, n)
    py = _RNG.uniform(0, 1, n)
    rise = _RNG.integers(1, 4, n).astype(float)
    sway_cyc = _RNG.integers(1, 4, n).astype(float)
    sway_amp = _RNG.uniform(0.006, 0.035, n)
    ph = _RNG.uniform(0, 2 * math.pi, n)
    rad = _RNG.integers(1, 5, n)
    bri = _RNG.uniform(100, 235, n)
    sprites = {r: _soft_sprite(int(r)) for r in set(rad.tolist())}

    def make(t):
        f = _frac(t)
        frame = np.zeros((H, W, 3), np.float32)
        ys = (py - f * rise) % 1.0
        xs = (px + sway_amp * np.sin(
            2 * math.pi * sway_cyc * f + ph
        )) % 1.0
        for i in range(n):
            pulse = 0.55 + 0.45 * math.sin(
                2 * math.pi * rise[i] * f + ph[i]
            ) ** 2
            b = bri[i] * pulse
            _splat(frame, int(xs[i] * W), int(ys[i] * H), sprites[rad[i]],
                   (b, b * 0.42, b * 0.08))
        return np.clip(frame, 0, 255).astype(np.uint8)
    return make


def _petals():
    """Soft rose petals falling and swaying across the scene."""
    n = 115
    px = _RNG.uniform(0, 1, n)
    py = _RNG.uniform(0, 1, n)
    fall = _RNG.integers(1, 3, n).astype(float)
    sway_cyc = _RNG.integers(1, 4, n).astype(float)
    sway_amp = _RNG.uniform(0.012, 0.055, n)
    ph = _RNG.uniform(0, 2 * math.pi, n)
    rx = _RNG.integers(3, 9, n)
    ry = _RNG.integers(6, 15, n)
    colors = _RNG.uniform(0, 1, n)
    sprites = [
        _soft_ellipse(int(rx[i]), int(ry[i]), float(ph[i])) for i in range(n)
    ]

    def make(t):
        f = _frac(t)
        frame = np.zeros((H, W, 3), np.float32)
        ys = (py + f * fall) % 1.0
        xs = (px + sway_amp * np.sin(
            2 * math.pi * sway_cyc * f + ph
        )) % 1.0
        for i in range(n):
            warm = colors[i]
            b = 145 + 65 * math.sin(
                2 * math.pi * sway_cyc[i] * f + ph[i]
            ) ** 2
            _splat(frame, int(xs[i] * W), int(ys[i] * H), sprites[i],
                   (b, b * (0.38 + 0.30 * warm), b * (0.52 + 0.25 * warm)))
        return np.clip(frame, 0, 255).astype(np.uint8)
    return make


def _confetti():
    """Colorful small pieces falling at different speeds."""
    n = 155
    px = _RNG.uniform(0, 1, n)
    py = _RNG.uniform(0, 1, n)
    fall = _RNG.integers(1, 4, n).astype(float)
    sway_cyc = _RNG.integers(1, 4, n).astype(float)
    sway_amp = _RNG.uniform(0.008, 0.04, n)
    ph = _RNG.uniform(0, 2 * math.pi, n)
    palettes = np.array([
        (235, 45, 145), (40, 205, 235), (255, 190, 35),
        (145, 75, 245), (55, 220, 125),
    ], dtype=np.float32)
    color_idx = _RNG.integers(0, len(palettes), n)
    sprites = [
        _soft_ellipse(
            int(_RNG.integers(2, 5)),
            int(_RNG.integers(5, 11)),
            float(ph[i]),
        )
        for i in range(n)
    ]

    def make(t):
        f = _frac(t)
        frame = np.zeros((H, W, 3), np.float32)
        ys = (py + f * fall) % 1.0
        xs = (px + sway_amp * np.sin(
            2 * math.pi * sway_cyc * f + ph
        )) % 1.0
        for i in range(n):
            # Screen blend needs luminous colors; keep them vivid but below
            # white so the underlying photo still shows through.
            _splat(frame, int(xs[i] * W), int(ys[i] * H), sprites[i],
                   palettes[color_idx[i]])
        return np.clip(frame, 0, 255).astype(np.uint8)
    return make


def _aurora():
    """Teal/violet curtains that undulate vertically."""
    y, x = np.mgrid[0:H, 0:W]
    xn = x.astype(np.float32) / max(1, W - 1)
    yn = y.astype(np.float32) / max(1, H - 1)
    phases = _RNG.uniform(0, 2 * math.pi, 3)
    centers = (0.28, 0.48, 0.67)
    colors = (
        np.array([12.0, 82.0, 62.0], dtype=np.float32),
        np.array([38.0, 30.0, 92.0], dtype=np.float32),
        np.array([8.0, 58.0, 88.0], dtype=np.float32),
    )

    def make(t):
        f = _frac(t)
        frame = np.zeros((H, W, 3), np.float32)
        for i in range(3):
            center = centers[i] + 0.10 * np.sin(
                xn * math.tau * (1.0 + i * 0.35)
                + math.tau * f * (i + 1)
                + phases[i]
            )
            ribbon = np.exp(-((yn - center) / (0.07 + i * 0.018)) ** 2)
            shimmer = 0.55 + 0.45 * np.sin(
                xn * math.tau * 2.0 - math.tau * f * (i + 1) + phases[i]
            ) ** 2
            frame += ribbon[..., None] * shimmer[..., None] * colors[i]
        return np.clip(frame, 0, 255).astype(np.uint8)
    return make


def _prism():
    """A broad rainbow light-leak sweep across the frame."""
    y, x = np.mgrid[0:H, 0:W]
    diagonal = (
        x.astype(np.float32) / max(1, W - 1)
        + 0.30 * y.astype(np.float32) / max(1, H - 1)
    )
    phase = float(_RNG.uniform(0, math.tau))

    def make(t):
        f = _frac(t)
        # Sine travel makes the band sweep left→right→left with no jump.
        center = 0.65 + 0.72 * math.sin(math.tau * f + phase)
        red = np.exp(-((diagonal - center + 0.075) / 0.095) ** 2)
        green = np.exp(-((diagonal - center) / 0.095) ** 2)
        blue = np.exp(-((diagonal - center - 0.075) / 0.095) ** 2)
        frame = np.stack(
            (red * 78.0, green * 58.0, blue * 88.0), axis=2
        )
        return np.clip(frame, 0, 255).astype(np.uint8)
    return make


def _film():
    """Analogue-film grain, dust pops and projector scratches."""
    total_frames = int(DUR * FPS)
    scratch_x = _RNG.uniform(0.05, 0.95, 7)
    scratch_phase = _RNG.uniform(0, math.tau, 7)
    scratch_cycles = _RNG.integers(1, 4, 7)
    scratch_width = _RNG.integers(1, 3, 7)

    def make(t):
        f = _frac(t)
        # Frame-indexed randomness stays lively while frame(DUR) == frame(0).
        frame_index = int(math.floor(f * total_frames + 1e-7)) % total_frames
        rng = np.random.default_rng(0xF11A + frame_index)
        frame = np.zeros((H, W, 3), np.float32)

        # Sparse grain: texture without a milky screen-blend veil.
        grain = rng.random((H // 3, W // 3), dtype=np.float32)
        grain = np.where(
            grain > 0.992,
            30.0 + (grain - 0.992) * 5000.0,
            0.0,
        )
        grain = np.repeat(np.repeat(grain, 3, axis=0), 3, axis=1)[:H, :W]
        frame += grain[..., None] * np.array((1.0, 0.91, 0.76), np.float32)

        for _ in range(34):
            x = int(rng.uniform(0, W))
            y = int(rng.uniform(0, H))
            r = int(rng.integers(1, 5))
            b = float(rng.uniform(60, 155))
            _splat(frame, x, y, _soft_sprite(r), (b, b * 0.9, b * 0.72))

        for i, base_x in enumerate(scratch_x):
            visibility = math.sin(
                math.tau * scratch_cycles[i] * f + scratch_phase[i]
            )
            if visibility < 0.58:
                continue
            x = int((
                base_x + 0.025 * math.sin(math.tau * f + scratch_phase[i])
            ) * W)
            b = 28.0 + 52.0 * (visibility - 0.58) / 0.42
            x0 = max(0, x)
            x1 = min(W, x + int(scratch_width[i]))
            if x1 > x0:
                frame[:, x0:x1, :] += np.array((b, b * 0.88, b * 0.68))

        # Gentle projector exposure flutter.
        frame += 3.0 + 4.0 * (
            0.5 + 0.5 * math.sin(math.tau * 3 * f + 0.7)
        )
        return np.clip(frame, 0, 255).astype(np.uint8)
    return make


def _scanlines():
    """Retro CRT scanlines with a slow rolling cyan/magenta light band."""
    y, x = np.mgrid[0:H, 0:W]
    yn = y.astype(np.float32) / max(1, H - 1)
    xn = x.astype(np.float32) / max(1, W - 1)
    # Three source pixels survive the 1080p→preview downscale as a crisp,
    # subtle line; one-pixel lines disappeared entirely in the picker card.
    lines = ((y % 12) < 3).astype(np.float32)

    def make(t):
        f = _frac(t)
        roll_dist = np.minimum(np.abs(yn - f), 1.0 - np.abs(yn - f))
        roll = np.exp(-((roll_dist / 0.075) ** 2))
        chroma = 0.5 + 0.5 * np.sin(
            math.tau * (xn * 1.5 + yn * 0.35 - f)
        )
        base = lines * 12.0
        frame = np.stack(
            (
                base + roll * (13.0 + 15.0 * chroma),
                base + roll * 9.0,
                base + roll * (28.0 - 12.0 * chroma),
            ),
            axis=2,
        )
        return np.clip(frame, 0, 255).astype(np.uint8)
    return make


def _fog():
    """Low, slow banks of pale mist for otherwise motionless photos."""
    y, x = np.mgrid[0:H, 0:W]
    xn = x.astype(np.float32) / max(1, W - 1)
    yn = y.astype(np.float32) / max(1, H - 1)
    phases = _RNG.uniform(0, math.tau, 4)
    centers_y = (0.48, 0.62, 0.77, 0.90)
    widths = (0.24, 0.20, 0.27, 0.22)

    def make(t):
        f = _frac(t)
        density = np.zeros((H, W), np.float32)
        for i in range(4):
            ridge = centers_y[i] + 0.085 * np.sin(
                math.tau * (xn * (1.0 + i * 0.25) + f * (i + 1))
                + phases[i]
            )
            bank = np.exp(-((yn - ridge) / widths[i]) ** 2)
            breakup = 0.38 + 0.62 * np.sin(
                math.tau * (xn * (1.5 + i * 0.35) - f * (i + 1))
                + phases[i] * 0.6
            ) ** 2
            density += bank * breakup
        density = np.clip(density * 12.0, 0, 46)
        return np.stack(
            (density * 0.82, density * 0.92, density),
            axis=2,
        ).astype(np.uint8)
    return make


def _shapes():
    """Minimal graphic circles, squares and diamonds floating over a still."""
    n = 14
    px = _RNG.uniform(0.08, 0.92, n)
    # Keep the lyric-safe center clear. The reference's animated basketball
    # lives around the illustration rather than crossing every word.
    py = np.where(
        _RNG.random(n) < 0.5,
        _RNG.uniform(0.08, 0.24, n),
        _RNG.uniform(0.76, 0.92, n),
    )
    amp_x = _RNG.uniform(0.025, 0.11, n)
    amp_y = _RNG.uniform(0.015, 0.05, n)
    cyc_x = _RNG.integers(1, 4, n)
    cyc_y = _RNG.integers(1, 4, n)
    phase = _RNG.uniform(0, math.tau, n)
    size = _RNG.integers(18, 65, n)
    kind = _RNG.integers(0, 3, n)
    palette = (
        (105, 78, 145), (62, 118, 145), (145, 90, 60),
        (112, 112, 112),
    )

    def make(t):
        f = _frac(t)
        image = Image.new("RGB", (W, H), (0, 0, 0))
        draw = ImageDraw.Draw(image)
        for i in range(n):
            cx = int((px[i] + amp_x[i] * math.sin(
                math.tau * cyc_x[i] * f + phase[i]
            )) * W)
            cy = int((py[i] + amp_y[i] * math.cos(
                math.tau * cyc_y[i] * f + phase[i]
            )) * H)
            r = int(size[i])
            color = palette[i % len(palette)]
            line_w = max(2, r // 9)
            if kind[i] == 0:
                draw.ellipse(
                    (cx - r, cy - r, cx + r, cy + r),
                    outline=color, width=line_w,
                )
            elif kind[i] == 1:
                draw.rectangle(
                    (cx - r, cy - r, cx + r, cy + r),
                    outline=color, width=line_w,
                )
            else:
                draw.polygon(
                    ((cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)),
                    outline=color, width=line_w,
                )
        return np.asarray(image, dtype=np.uint8)
    return make


EFFECTS = {
    "snow": _snow, "rain": _rain, "stars": _stars,
    "bokeh": _bokeh, "light": _light, "aurora": _aurora,
    "dust": _dust, "embers": _embers, "petals": _petals,
    "prism": _prism, "confetti": _confetti, "film": _film,
    "scanlines": _scanlines, "fog": _fog, "shapes": _shapes,
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
        clip = VideoClip(make, duration=DUR)
        clip = clip.with_fps(FPS) if _MOVIEPY_V2 else clip.set_fps(FPS)
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
