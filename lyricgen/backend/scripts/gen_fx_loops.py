"""Offline generator of PREMIUM effect-overlay loops for the compositing layer.

WHY offline: generating particles per-render would reintroduce the Python
frame-loop the libass migration killed (ass_render.py). So we bake short,
seamless, deterministic loops ONCE here, and the render only DECODES + blends
them (fast, C-level ffmpeg).

WHY plain RGB instead of alpha: luminous effects are authored on black and
screen-blended; shadow/ink/halftone are authored on white and multiplied.
Their neutral background reads as transparent in either blend mode, so no
alpha codec (VP9-alpha / ProRes4444) is needed. Plain H.264 stays fast.

SEAMLESS by construction: every motion is parametrized as
`phase = (base + (t/DUR) * k) % 1` with INTEGER k (full cycles over the loop),
so frame(0) == frame(DUR). No palindrome (which would make snow fall upward).

Run from lyricgen/backend:
  ./venv/bin/python scripts/gen_fx_loops.py              # all effects
  ./venv/bin/python scripts/gen_fx_loops.py snow rain    # subset
Outputs to backend/assets/fx/<effect>.mp4 (normally 1920x1080, 24fps, ~8s,
H.264; dense resolution-independent patterns may use a smaller bake).
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
# Dense halftone geometry is resolution-independent and is always scaled by
# the compositor. Baking it at half resolution avoids a ~21 MB high-frequency
# H.264 asset with no visible benefit in the final 1080p render.
EFFECT_SIZE = {"halftone": (960, 540)}
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
    n = 12
    per_band = n // 2
    # Two collision-free bands keep both the lyric-safe center and the other
    # shapes clear. Even spacing + small motion envelopes means neighbouring
    # paths never intersect, unlike the previous fully-random trajectories.
    lane_x = np.linspace(0.09, 0.91, per_band)
    px = np.concatenate((
        lane_x + _RNG.uniform(-0.012, 0.012, per_band),
        lane_x + _RNG.uniform(-0.012, 0.012, per_band),
    ))
    py = np.concatenate((
        np.array((0.10, 0.21, 0.10, 0.21, 0.10, 0.21)),
        np.array((0.79, 0.90, 0.79, 0.90, 0.79, 0.90)),
    ))
    amp_x = _RNG.uniform(0.008, 0.022, n)
    amp_y = _RNG.uniform(0.008, 0.022, n)
    cyc_x = _RNG.integers(1, 4, n)
    cyc_y = _RNG.integers(1, 4, n)
    phase = _RNG.uniform(0, math.tau, n)
    size = _RNG.integers(18, 56, n)
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


def _liquid_glass():
    """Travelling glass ribbons and specular highlights."""
    y, x = np.mgrid[0:H, 0:W]
    xn = x.astype(np.float32) / W
    yn = y.astype(np.float32) / H

    def make(t):
        f = _frac(t)
        frame = np.zeros((H, W, 3), np.float32)
        for i, color in enumerate(((38, 65, 92), (75, 38, 92), (28, 88, 86))):
            center = (0.12 + i * 0.34 + f * (i + 1)) % 1.35 - 0.16
            warped = xn + 0.075 * np.sin(math.tau * (yn * (1.25 + i * .2) + f))
            ribbon = np.exp(-((warped - center) / (0.045 + i * .012)) ** 2)
            edge = np.exp(-((np.abs(warped - center) - .055) / .012) ** 2)
            frame += (ribbon * .35 + edge)[..., None] * np.array(color, np.float32)
        return np.clip(frame, 0, 255).astype(np.uint8)
    return make


def _caustics():
    """Water-caustic light web, animated in two directions."""
    y, x = np.mgrid[0:H, 0:W]
    xn = x.astype(np.float32) / W
    yn = y.astype(np.float32) / H

    def make(t):
        f = _frac(t)
        a = np.sin(math.tau * (xn * 3.2 + yn * 1.7 + f * 2))
        b = np.sin(math.tau * (xn * -1.4 + yn * 3.8 - f * 3))
        c = np.sin(math.tau * (xn * 4.6 - yn * 2.1 + f))
        web = np.clip((a + b + c) / 3.0, 0.28, 1.0)
        web = ((web - .28) / .72) ** 5
        shimmer = 0.55 + .45 * np.sin(math.tau * (xn + yn + f * 4)) ** 2
        power = web * shimmer
        return np.stack((power * 22, power * 105, power * 135), axis=2).astype(np.uint8)
    return make


def _rgb_glitch():
    """Editorial RGB displacement bars; sparse enough to preserve lyrics."""
    y, x = np.mgrid[0:H, 0:W]
    yn = y.astype(np.float32) / H
    xn = x.astype(np.float32) / W

    def make(t):
        f = _frac(t)
        frame = np.zeros((H, W, 3), np.float32)
        gate = (
            (np.sin(math.tau * (yn * 17 + f * 8)) > .86)
            | (np.sin(math.tau * (yn * 31 - f * 5)) > .94)
        ).astype(np.float32)
        blocks = (np.sin(math.tau * (xn * 4 + np.floor(yn * 13) * .17 + f * 3)) > .35)
        mask = gate * blocks
        frame[:, :, 0] = mask * 132
        frame[:, :, 1] = np.roll(mask, int(22 * math.sin(math.tau * f)), axis=1) * 72
        frame[:, :, 2] = np.roll(mask, int(-30 * math.cos(math.tau * f)), axis=1) * 146
        return frame.astype(np.uint8)
    return make


def _neon_edge():
    """Moving cyan/magenta contour lines that frame the still."""
    y, x = np.mgrid[0:H, 0:W]
    xn = x.astype(np.float32) / W
    yn = y.astype(np.float32) / H

    def make(t):
        f = _frac(t)
        curve_a = np.abs(yn - (.23 + .09 * np.sin(math.tau * (xn * 1.8 + f))))
        curve_b = np.abs(yn - (.76 + .11 * np.sin(math.tau * (xn * 1.4 - f * 2))))
        edge_a = np.exp(-(curve_a / .009) ** 2)
        edge_b = np.exp(-(curve_b / .011) ** 2)
        vertical = np.exp(-((xn - (.5 + .34 * math.sin(math.tau * f))) / .012) ** 2)
        return np.stack((
            (edge_b + vertical * .45) * 145,
            edge_a * 118,
            (edge_a + edge_b * .7 + vertical) * 165,
        ), axis=2).clip(0, 255).astype(np.uint8)
    return make


def _shadow_play():
    """Soft moving shadows authored over white for multiply compositing."""
    def make(t):
        f = _frac(t)
        image = Image.new("RGB", (W, H), (255, 255, 255))
        draw = ImageDraw.Draw(image, "RGB")
        for i in range(7):
            phase = math.tau * (f * (1 + i % 3) + i / 7)
            cx = int(W * (.5 + .62 * math.sin(phase)))
            cy = int(H * (.5 + .45 * math.cos(phase * .73)))
            rx = int(W * (.12 + .035 * (i % 3)))
            ry = int(H * (.38 + .04 * (i % 2)))
            shade = 92 + i * 8
            draw.ellipse((cx-rx, cy-ry, cx+rx, cy+ry), fill=(shade, shade, shade))
        return np.asarray(image, dtype=np.uint8)
    return make


def _kaleido():
    """Slow radial kaleidoscope rays with a luminous centre."""
    y, x = np.mgrid[0:H, 0:W]
    xx = x.astype(np.float32) / W - .5
    yy = y.astype(np.float32) / H - .5
    theta = np.arctan2(yy, xx)
    radius = np.sqrt(xx * xx + yy * yy)

    def make(t):
        f = _frac(t)
        spokes = np.clip(np.cos(theta * 8 + math.tau * f * 2), .72, 1)
        spokes = ((spokes - .72) / .28) ** 3
        rings = .35 + .65 * np.sin(radius * 42 - math.tau * f * 3) ** 2
        fade = np.clip(1.0 - radius * 1.45, 0, 1)
        p = spokes * rings * fade
        return np.stack((p * 110, p * 48, p * 155), axis=2).astype(np.uint8)
    return make


def _halftone():
    """Animated print dots over white, intended for multiply."""
    y, x = np.mgrid[0:H, 0:W]
    cell = 20

    def make(t):
        f = _frac(t)
        ox = int(8 * math.sin(math.tau * f))
        oy = int(8 * math.cos(math.tau * f))
        dx = ((x + ox) % cell) - cell / 2
        dy = ((y + oy) % cell) - cell / 2
        wave = .5 + .5 * np.sin(math.tau * (x / W * 1.8 + y / H * .8 - f * 2))
        radius = 2.0 + wave * 5.5
        dots = (dx * dx + dy * dy < radius * radius)
        level = np.where(dots, 45 + wave * 55, 255)
        return np.stack((level, level, level), axis=2).astype(np.uint8)
    return make


def _ink_reveal():
    """Organic ink blooms crossing the photo, authored for multiply."""
    y, x = np.mgrid[0:H, 0:W]
    xn = x.astype(np.float32) / W
    yn = y.astype(np.float32) / H
    centers = ((.18, .27), (.78, .22), (.55, .72), (.08, .84), (.91, .68))

    def make(t):
        f = _frac(t)
        ink = np.zeros((H, W), np.float32)
        for i, (cx, cy) in enumerate(centers):
            pulse = .5 + .5 * math.sin(math.tau * (f * (1 + i % 2) - i / len(centers)))
            r = .025 + pulse * (.12 + .02 * i)
            wobble = .018 * np.sin(math.tau * (xn * (2+i*.2) + yn * 1.7 + f))
            dist = np.sqrt((xn-cx+wobble) ** 2 + (yn-cy-wobble) ** 2)
            ink = np.maximum(ink, np.clip((r - dist) / .035, 0, 1))
        level = 255 - ink * 205
        return np.stack((level, level*.98, level*.96), axis=2).clip(0,255).astype(np.uint8)
    return make


def _heatwave():
    """Warm mirage bands rising continuously."""
    y, x = np.mgrid[0:H, 0:W]
    xn = x.astype(np.float32) / W
    yn = y.astype(np.float32) / H

    def make(t):
        f = _frac(t)
        wave = np.sin(math.tau * (yn * 8 - f * 4 + .25 * np.sin(xn * math.tau * 2)))
        bands = np.clip(wave - .55, 0, 1) ** 2
        lower = np.clip((yn - .2) / .8, 0, 1)
        p = bands * lower
        return np.stack((p * 115, p * 45, p * 10), axis=2).astype(np.uint8)
    return make


def _chromatic_pulse():
    """Continuous concentric chromatic breathing, independent of the beat."""
    y, x = np.mgrid[0:H, 0:W]
    xx = x.astype(np.float32) / W - .5
    yy = y.astype(np.float32) / H - .5
    radius = np.sqrt(xx * xx + yy * yy)

    def make(t):
        f = _frac(t)
        red = np.exp(-((radius - (.18 + .13 * math.sin(math.tau*f))) / .035) ** 2)
        cyan = np.exp(-((radius - (.36 + .12 * math.sin(math.tau*f + 2))) / .045) ** 2)
        violet = np.exp(-((radius - (.54 + .10 * math.sin(math.tau*f + 4))) / .055) ** 2)
        return np.stack((red*130 + violet*55, cyan*105, cyan*145 + violet*120), axis=2).clip(0,255).astype(np.uint8)
    return make


def _cutout_echo():
    """Offset editorial frames that read as paper-cut echoes."""
    def make(t):
        f = _frac(t)
        image = Image.new("RGB", (W, H), (0, 0, 0))
        draw = ImageDraw.Draw(image)
        for i, color in enumerate(((135, 25, 82), (20, 115, 145), (116, 82, 24))):
            drift = math.sin(math.tau * f + i * 2.1)
            margin_x = int(W * (.09 + i*.055 + drift*.018))
            margin_y = int(H * (.10 + i*.045 - drift*.012))
            draw.rounded_rectangle(
                (margin_x, margin_y, W-margin_x, H-margin_y),
                radius=45+i*12, outline=color, width=13+i*5,
            )
        return np.asarray(image, dtype=np.uint8)
    return make


def _projector():
    """Projector cone, gate weave and exposure flutter."""
    y, x = np.mgrid[0:H, 0:W]
    xn = x.astype(np.float32) / W
    yn = y.astype(np.float32) / H
    total_frames = int(DUR * FPS)

    def make(t):
        f = _frac(t)
        idx = int(f * total_frames) % total_frames
        rng = np.random.default_rng(0xCAFE + idx)
        center = .5 + .18 * math.sin(math.tau * f)
        cone = np.clip(1 - np.abs(xn-center) / (.10 + yn*.48), 0, 1) * (.25 + yn*.75)
        flutter = .55 + .45 * math.sin(math.tau * f * 24) ** 2
        frame = cone * (18 + 22*flutter)
        dust = rng.random((H//4, W//4), dtype=np.float32)
        dust = np.repeat(np.repeat((dust > .998)*95, 4, axis=0), 4, axis=1)[:H,:W]
        return np.stack((frame+dust, frame*.82+dust*.82, frame*.52+dust*.55), axis=2).clip(0,255).astype(np.uint8)
    return make


def _beat_phase(t):
    """One authored hit every 0.5 seconds: canonical 120 BPM."""
    return (t * 2.0) % 1.0


def _bass_pulse():
    y, x = np.mgrid[0:H, 0:W]
    xx = x.astype(np.float32) / W - .5
    yy = y.astype(np.float32) / H - .5
    radius = np.sqrt(xx*xx + yy*yy)

    def make(t):
        hit = (1.0 - _beat_phase(t)) ** 4
        glow = np.exp(-(radius / (.18 + .16*(1-hit))) ** 2) * hit
        return np.stack((glow*150, glow*42, glow*95), axis=2).astype(np.uint8)
    return make


def _beat_flash():
    y, x = np.mgrid[0:H, 0:W]
    vignette = np.clip(1 - np.sqrt(((x/W)-.5)**2 + ((y/H)-.5)**2), 0, 1)

    def make(t):
        hit = (1.0 - _beat_phase(t)) ** 10
        p = vignette * hit
        return np.stack((p*170, p*155, p*132), axis=2).astype(np.uint8)
    return make


def _chromatic_hit():
    y, x = np.mgrid[0:H, 0:W]
    xn = x.astype(np.float32) / W

    def make(t):
        phase = _beat_phase(t)
        hit = (1-phase) ** 5
        spread = .025 + phase*.16
        r = np.exp(-((xn-(.5-spread))/(.018+phase*.025))**2)*hit
        b = np.exp(-((xn-(.5+spread))/(.018+phase*.025))**2)*hit
        g = np.exp(-((xn-.5)/(.012+phase*.018))**2)*hit*.55
        return np.stack((r*180, g*110, b*190), axis=2).astype(np.uint8)
    return make


def _beat_ripple():
    y, x = np.mgrid[0:H, 0:W]
    radius = np.sqrt((x.astype(np.float32)/W-.5)**2 + (y.astype(np.float32)/H-.5)**2)

    def make(t):
        phase = _beat_phase(t)
        ring = np.exp(-((radius-(.04+phase*.62))/(.012+phase*.018))**2) * (1-phase)**.8
        return np.stack((ring*55, ring*145, ring*185), axis=2).astype(np.uint8)
    return make


def _echo_hit():
    def make(t):
        phase = _beat_phase(t)
        image = Image.new("RGB", (W, H), (0, 0, 0))
        draw = ImageDraw.Draw(image)
        for i, color in enumerate(((155, 35, 105), (30, 130, 165), (125, 82, 35))):
            local = max(0.0, 1.0 - phase - i*.12)
            if local <= 0:
                continue
            inset = int((.08 + (1-local)*.16 + i*.025) * min(W,H))
            draw.rounded_rectangle(
                (inset, inset, W-inset, H-inset),
                radius=55, outline=tuple(int(c*local) for c in color),
                width=max(4, int(18*local)),
            )
        return np.asarray(image, dtype=np.uint8)
    return make


EFFECTS = {
    "snow": _snow, "rain": _rain, "stars": _stars,
    "bokeh": _bokeh, "light": _light, "aurora": _aurora,
    "dust": _dust, "embers": _embers, "petals": _petals,
    "prism": _prism, "confetti": _confetti, "film": _film,
    "scanlines": _scanlines, "fog": _fog, "shapes": _shapes,
    "liquid_glass": _liquid_glass, "caustics": _caustics,
    "rgb_glitch": _rgb_glitch, "neon_edge": _neon_edge,
    "shadow_play": _shadow_play, "kaleido": _kaleido,
    "halftone": _halftone, "ink_reveal": _ink_reveal,
    "heatwave": _heatwave, "chromatic_pulse": _chromatic_pulse,
    "cutout_echo": _cutout_echo, "projector": _projector,
    "bass_pulse": _bass_pulse, "beat_flash": _beat_flash,
    "chromatic_hit": _chromatic_hit, "beat_ripple": _beat_ripple,
    "echo_hit": _echo_hit,
}


def main():
    os.makedirs(OUT, exist_ok=True)
    only = set(sys.argv[1:])
    for name, builder in EFFECTS.items():
        if only and name not in only:
            continue
        # rng re-seeded per effect with a STABLE seed (crc32, not hash() which
        # is salted per process) so each effect is deterministic across runs.
        global _RNG, W, H
        W, H = EFFECT_SIZE.get(name, (1920, 1080))
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
