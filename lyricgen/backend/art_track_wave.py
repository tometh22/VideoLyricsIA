"""Audio-reactive EQ bars for the art-track ("official audio") composite.

The art-track render needs bars that PULSE IN PLACE following the beat —
not a scrolling oscilloscope (showwaves) nor a raw spectrum (showfreqs
stacks every tall bar on the bass side). This module precomputes per-frame
bar heights from the audio (mel bands → per-band normalization → fast
attack / slow release envelope) and draws each frame's waveform strip as a
small RGBA image that the render pipeline feeds to ffmpeg as an image2
sequence overlaid on the static base.

Pure numpy/librosa/PIL — no ffmpeg, no pipeline imports — so the DSP and
drawing math is unit-testable without rendering video. The pipeline owns
all side-effect policy (where frames live, cleanup, fallback logging).

Key design points (from the motion-design review):
- One mel band per bar, each band normalized against its own global p98
  over the analysis window → heights are balanced across the width (no
  bass wall on the left) while motion stays frequency-differentiated.
- Global (not rolling) normalization preserves quiet-verse→chorus
  dynamics: a quiet intro renders LOW bars, the chorus fills the box.
- Asymmetric attack/release at video rate: a hit raises a bar in 1-2
  frames, then it falls back with "gravity" over ~0.45 s.
- Mirrored-from-center magnitude bars with rounded caps and a minimum
  "spine" so silence draws a thin center line, never an empty box.
"""

from __future__ import annotations

import math
import os
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import numpy as np

# ---------------------------------------------------------------------------
# DSP: audio → per-frame bar heights
# ---------------------------------------------------------------------------

# Analysis constants (see plan: sr high enough for treble bands, hop ~12ms)
_SR = 22050
_N_FFT = 2048
_HOP = 512
_FMIN = 30.0
_FMAX = 10000.0

# Height mapping: dynamic range in dB below each band's p98 reference, and
# a >1 gamma so quiet passages stay visibly low while choruses reach ~1.0.
# Tuned on a real master (Rata Blanca): D=40/γ=1.25 compressed everything
# into the top (intro p50≈0.74 vs chorus 0.77 — no breathing, ~2.5%
# frame-to-frame motion). D=25/γ=1.6 doubles the height response per dB.
_TOP_DB = 25.0
_PCTL = 98.0
_DEAD_BAND_GUARD_DB = 25.0
_GAMMA = 1.6

# Attack/release time constants in SECONDS (fps-normalized in _attack_release
# so 23.976/25/30 fps all decay at the same real-time rate).
_TAU_ATTACK = 0.015
_TAU_RELEASE = 0.18


def _band_db(y: np.ndarray, sr: int, n_bars: int):
    """Mel-band energies in dB. Returns (S_db [n_bars, T], times [T])."""
    import librosa
    S = librosa.feature.melspectrogram(
        y=y, sr=sr, n_fft=_N_FFT, hop_length=_HOP,
        n_mels=n_bars, fmin=_FMIN, fmax=_FMAX, power=2.0,
    )
    S_db = librosa.power_to_db(S, ref=1.0, top_db=None)
    times = librosa.frames_to_time(np.arange(S.shape[1]), sr=sr, hop_length=_HOP)
    return S_db.astype(np.float32), times.astype(np.float32)


def _per_band_normalize(S_db: np.ndarray, *, top_db: float = _TOP_DB,
                        pctl: float = _PCTL,
                        dead_band_guard: float = _DEAD_BAND_GUARD_DB,
                        gamma: float = _GAMMA) -> np.ndarray:
    """Map per-band dB to [0,1] heights, each band against its OWN p98.

    Per-band reference equalizes bass vs treble HEIGHTS (the fix for the
    "wall of bass on the left" that killed plain showfreqs) while keeping
    each band's temporal dynamics. The dead-band guard stops near-silent
    top mels (dark masters) from amplifying hiss to full height: a band
    whose peak sits more than `dead_band_guard` dB below the hottest
    band's peak is normalized against that floor instead, so it renders
    low. Global (not rolling) percentile → quiet intros stay low.
    """
    ref = np.percentile(S_db, pctl, axis=1)          # (n_bars,)
    ref = np.maximum(ref, ref.max() - dead_band_guard)
    lin = (S_db - (ref[:, None] - top_db)) / top_db
    lin = np.clip(lin, 0.0, 1.0)
    return np.power(lin, gamma, dtype=np.float32)


def _resample_to_frames(bands: np.ndarray, times: np.ndarray,
                        n_frames: int, fps: float) -> np.ndarray:
    """Interp band heights [n_bars, T] at audio-hop rate onto exactly
    `n_frames` video frame midpoints. Returns [n_frames, n_bars]."""
    t_video = (np.arange(n_frames, dtype=np.float64) + 0.5) / fps
    out = np.empty((n_frames, bands.shape[0]), dtype=np.float32)
    for b in range(bands.shape[0]):
        out[:, b] = np.interp(t_video, times, bands[b])
    return out


def _attack_release(frames: np.ndarray, fps: float, *,
                    tau_a: float = _TAU_ATTACK,
                    tau_r: float = _TAU_RELEASE) -> np.ndarray:
    """Asymmetric envelope over time (axis 0): fast attack, slow release.

    attack/release coefficients are derived from time constants in seconds
    so the motion feels identical at any fps. A transient raises a bar to
    ~target within 1-2 frames; afterwards it falls with "gravity" to ~10%
    in roughly 2.5*tau_r seconds.
    """
    attack = 1.0 - math.exp(-1.0 / (fps * tau_a))
    release = 1.0 - math.exp(-1.0 / (fps * tau_r))
    out = np.empty_like(frames)
    env = frames[0].copy()
    out[0] = env
    for i in range(1, len(frames)):
        x = frames[i]
        coeff = np.where(x > env, attack, release)
        env = env + coeff * (x - env)
        out[i] = env
    return out


def _spatial_smooth(frames: np.ndarray) -> np.ndarray:
    """One [0.2, 0.6, 0.2] pass across the bar axis — kills single-bar
    strobing without blurring the EQ shape. Edge bars reuse themselves."""
    padded = np.concatenate(
        [frames[:, :1], frames, frames[:, -1:]], axis=1)
    return (0.2 * padded[:, :-2] + 0.6 * padded[:, 1:-1]
            + 0.2 * padded[:, 2:]).astype(np.float32)


def static_bar_frames(n_frames: int, n_bars: int, fps: float,
                      mp3_path: str | None = None) -> np.ndarray:
    """Degraded-mode heights: the whole-song RMS envelope (if the audio is
    readable) or flat 0.25 bars, with a gentle sine "breathing" so even the
    fallback video has life. Never raises."""
    base = np.full(n_bars, 0.25, dtype=np.float32)
    if mp3_path:
        try:
            import librosa
            y, sr = librosa.load(mp3_path, sr=8000, mono=True)
            if len(y):
                hop = max(1, len(y) // (n_bars * 4))
                rms = librosa.feature.rms(y=y, frame_length=hop * 2,
                                          hop_length=hop)[0]
                idx = np.linspace(0, len(rms), n_bars + 1).astype(int)
                env = np.array([
                    rms[idx[i]:idx[i + 1]].mean() if idx[i + 1] > idx[i] else 0.0
                    for i in range(n_bars)
                ])
                peak = env.max() or 1.0
                base = np.power(env / peak, 0.7).astype(np.float32)
        except Exception:
            pass
    i = np.arange(n_frames, dtype=np.float32)[:, None]
    b = np.arange(n_bars, dtype=np.float32)[None, :]
    breathe = 1.0 + 0.08 * np.sin(2 * np.pi * 0.8 * i / fps + b * 0.4)
    return np.clip(base[None, :] * breathe, 0.0, 1.0).astype(np.float32)


def compute_bar_frames(mp3_path: str, *, n_bars: int, n_frames: int,
                       fps: float, win_start: float = 0.0,
                       win_dur: float | None = None) -> np.ndarray:
    """Per-frame bar heights [n_frames, n_bars] in [0,1] for the audio
    window. Falls back to `static_bar_frames` on ANY failure so the render
    never breaks (the caller logs; a degraded waveform beats a dead job)."""
    try:
        import librosa
        y, sr = librosa.load(mp3_path, sr=_SR, mono=True,
                             offset=max(0.0, win_start), duration=win_dur)
        if not len(y):
            raise ValueError("empty audio window")
        S_db, times = _band_db(y, sr, n_bars)
        heights = _per_band_normalize(S_db)
        frames = _resample_to_frames(heights, times, n_frames, fps)
        frames = _attack_release(frames, fps)
        return _spatial_smooth(frames)
    except Exception:
        return static_bar_frames(n_frames, n_bars, fps, mp3_path)


# ---------------------------------------------------------------------------
# Drawing: heights → RGBA strip
# ---------------------------------------------------------------------------

_CORE_ALPHA = 235
_GLOW_ALPHA = 55
_GLOW_PAD_MAX = 3


def _strip_geometry(n_bars: int, w: int, h: int, pitch: int, bar_w: int):
    """Shared slot math for the reference and sprite paths."""
    x_off = max(0, (w - n_bars * pitch) // 2)
    min_half = max(2, round(h * 0.035))
    glow_pad = min(_GLOW_PAD_MAX, (pitch - bar_w) // 2)
    return x_off, min_half, glow_pad


def quantize_half(height: float, h: int, min_half: int) -> int:
    """Bar half-height in px for a [0,1] height value."""
    return max(min_half, int(round(float(height) * (h / 2 - 1))))


def _draw_bar(draw, x0: int, h: int, half: int, bar_w: int, glow_pad: int):
    """One mirrored bar: glow underlay + rounded-cap core.

    Rows are integer-symmetric around the h//2 midline (`cy-half` to
    `cy+half-1`, both inclusive in PIL) so the strip mirrors exactly —
    a float centerline on an even-height image leaves a one-row bias
    that reads as the whole waveform sitting a hair low.
    """
    cy = h // 2
    if glow_pad > 0:
        draw.rounded_rectangle(
            [x0 - glow_pad, cy - half - glow_pad,
             x0 + bar_w + glow_pad, cy + half - 1 + glow_pad],
            radius=min((bar_w + 2 * glow_pad) // 2, half + glow_pad),
            fill=(255, 255, 255, _GLOW_ALPHA))
    draw.rounded_rectangle(
        [x0, cy - half, x0 + bar_w, cy + half - 1],
        radius=min(bar_w // 2, half), fill=(255, 255, 255, _CORE_ALPHA))


def draw_wave_frame(heights, w: int, h: int, *, pitch: int, bar_w: int):
    """Reference implementation: mirrored-from-center EQ bars as an RGBA
    strip. Used directly for one-off frames (thumbnail) and as the parity
    oracle for the sprite fast path in tests."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    n_bars = len(heights)
    x_off, min_half, glow_pad = _strip_geometry(n_bars, w, h, pitch, bar_w)
    slot_margin = (pitch - bar_w) // 2
    for i, a in enumerate(heights):
        half = quantize_half(a, h, min_half)
        x0 = x_off + i * pitch + slot_margin
        _draw_bar(d, x0, h, half, bar_w, glow_pad)
    return img


class WaveFrameWriter:
    """Sprite-cached strip assembler.

    Bar half-heights quantize to ~h/2 distinct integers, so each distinct
    bar column (glow + core) is drawn ONCE with the same PIL code as
    `draw_wave_frame` into a pitch-wide sprite, and frames are assembled
    by numpy column assignment (~1 ms/frame vs ~5 ms drawing every rect).
    """

    def __init__(self, n_bars: int, w: int, h: int, *, pitch: int, bar_w: int):
        self.n_bars, self.w, self.h = n_bars, w, h
        self.pitch, self.bar_w = pitch, bar_w
        self.x_off, self.min_half, self.glow_pad = _strip_geometry(
            n_bars, w, h, pitch, bar_w)
        self._sprites: dict[int, np.ndarray] = {}

    def _sprite(self, half: int) -> np.ndarray:
        """Alpha plane (h, pitch) for one bar of the given half-height."""
        cached = self._sprites.get(half)
        if cached is not None:
            return cached
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (self.pitch, self.h), (0, 0, 0, 0))
        _draw_bar(ImageDraw.Draw(img), (self.pitch - self.bar_w) // 2,
                  self.h, half, self.bar_w, self.glow_pad)
        alpha = np.asarray(img)[:, :, 3].copy()
        self._sprites[half] = alpha
        return alpha

    def frame(self, heights):
        """Assemble one RGBA strip image for a heights vector."""
        from PIL import Image
        rgba = np.zeros((self.h, self.w, 4), dtype=np.uint8)
        rgba[:, :, :3] = 255
        for i, a in enumerate(heights):
            half = quantize_half(a, self.h, self.min_half)
            x0 = self.x_off + i * self.pitch
            rgba[:, x0:x0 + self.pitch, 3] = self._sprite(half)
        return Image.fromarray(rgba, "RGBA")


def write_wave_frames(frames: np.ndarray, out_dir: str, *, w: int, h: int,
                      pitch: int, bar_w: int) -> str:
    """Write the strip sequence as w000000.png… under `out_dir` (created if
    needed). Returns the ffmpeg image2 pattern path. PNG compression
    (zlib, GIL-released) runs on a small thread pool with bounded
    in-flight images so memory stays flat at any track length. The caller
    owns cleanup of `out_dir`."""
    os.makedirs(out_dir, exist_ok=True)
    writer = WaveFrameWriter(frames.shape[1], w, h, pitch=pitch, bar_w=bar_w)
    with ThreadPoolExecutor(max_workers=3) as pool:
        pending = deque()
        for i in range(len(frames)):
            img = writer.frame(frames[i])
            path = os.path.join(out_dir, f"w{i:06d}.png")
            pending.append(pool.submit(img.save, path, compress_level=1))
            while len(pending) > 128:
                pending.popleft().result()
        while pending:
            pending.popleft().result()
    return os.path.join(out_dir, "w%06d.png")
