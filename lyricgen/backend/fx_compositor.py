"""Effect-overlay + color-grade compositing for the single-pass lyric render.

The render burns lyrics with libass in ONE ffmpeg pass (see pipeline.py
`_render_lyrics_ass`). This module builds the video filter for that pass,
optionally adding two layers BEFORE the subtitles burn:

  1. EFFECT layer (snow / liquid glass / shadows / beat ripples / ...): a
     pre-baked seamless RGB loop at assets/fx/<effect>.mp4 (built once by
     scripts/gen_fx_loops.py). Luminous loops are screen-blended over black;
     shadow/ink treatments are multiplied over white. Neither needs an alpha
     codec, so plain H.264 stays fast to decode.

     CRITICAL: the blend MUST run in RGB (`format=gbrp`). In YUV, `blend=screen`
     operates on the chroma (U/V) planes and tints the whole frame magenta.
     Output is converted back to yuv420p before the subtitles burn. (Verified
     2026-05-22 — the magenta bug.)

  2. GRADE (eq): a real post color grade derived from the palette, so `style`
     finally affects the rendered pixels (until now it only nudged the prompt).

Layer order in one pass:  bg → [effect screen-blend] → [grade] → subtitles.

This module is intentionally a leaf (no pipeline import) so it stays unit-
testable without moviepy/ffmpeg. The caller (pipeline) splices the returned
filter + extra inputs into its ffmpeg command.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from functools import lru_cache

logger = logging.getLogger("genly.fx_compositor")

# Pre-baked effect loops. They MUST ship inside this module's own package
# (lyricgen/backend/assets/fx) so they land in the Docker build context — the
# image is built from lyricgen/backend/ (`COPY . .`), exactly like fonts/ live
# in backend/fonts/. The legacy repo location (lyricgen/assets/fx, a SIBLING of
# backend/) is kept ONLY as a local-dev fallback: it sits OUTSIDE the build
# context, so it never made it into the image and EVERY effect silently no-op'd
# in prod until 2026-06-04. Mirror of _FONTS_DIR's candidate resolution.
_FX_DIR_CANDIDATES = [
    os.path.join(os.path.dirname(__file__), "assets", "fx"),        # shipped (in-image)
    os.path.join(os.path.dirname(__file__), "..", "assets", "fx"),  # legacy repo/local
]
_FX_DIR = next(
    (p for p in _FX_DIR_CANDIDATES if os.path.isdir(p)),
    _FX_DIR_CANDIDATES[0],
)

# Available pre-baked effect loops (must match scripts/gen_fx_loops.py and the
# wizard catalogue). Keep this tuple explicit: it is the server-side allowlist
# that prevents arbitrary filenames from reaching the ffmpeg command.
EFFECTS = (
    "snow",
    "rain",
    "stars",
    "bokeh",
    "light",
    "aurora",
    "dust",
    "embers",
    "petals",
    "prism",
    "confetti",
    "film",
    "scanlines",
    "fog",
    "shapes",
    # Motion Lab — effects designed specifically to give fixed photos a
    # distinct visual language (rather than another particle colourway).
    "liquid_glass",
    "caustics",
    "rgb_glitch",
    "neon_edge",
    "shadow_play",
    "kaleido",
    "halftone",
    "ink_reveal",
    "heatwave",
    "chromatic_pulse",
    "cutout_echo",
    "projector",
    # Generative-first: the pipeline animates one semantic region of the
    # selected/generated photo through Veo image-to-video.  This baked mask is
    # the deterministic fallback, so the option still moves pixels when the
    # provider is unavailable.
    "foto_viva",
    # Audio-reactive pack. The baked loops are authored at 120 BPM and their
    # PTS is tempo-matched to the detected song BPM at render time.
    "bass_pulse",
    "beat_flash",
    "chromatic_hit",
    "beat_ripple",
    "echo_hit",
)

REACTIVE_EFFECTS = (
    "bass_pulse",
    "beat_flash",
    "chromatic_hit",
    "beat_ripple",
    "echo_hit",
)

GENERATIVE_EFFECTS = (
    "foto_viva",
)

# These treatments derive their visible pixels from the selected photo.  The
# baked MP4 is only an auxiliary light/mask layer.  Keeping this allow-list
# explicit lets the editor describe the effect honestly and gives tests a
# stable contract: these are transformations, not decorative overlays.
PIXEL_TRANSFORM_EFFECTS = (
    "liquid_glass",
    "rgb_glitch",
    "neon_edge",
    "kaleido",
    "halftone",
    "ink_reveal",
    "heatwave",
    "chromatic_pulse",
    "cutout_echo",
    "projector",
    "foto_viva",
)

# Most loops emit light over black and therefore use screen. These three are
# deliberately authored as dark ink/shadow over white, so multiply gives fixed
# photos motion without washing their highlights.
_FX_BLEND = {
    "shadow_play": "multiply",
    "halftone": "multiply",
    "ink_reveal": "multiply",
}

# Keep the more graphic treatments editorial rather than overpowering lyrics.
_FX_OPACITY = {
    "rgb_glitch": 0.44,
    "neon_edge": 0.52,
    "shadow_play": 0.34,
    "kaleido": 0.68,
    "halftone": 0.46,
    "ink_reveal": 0.56,
    "heatwave": 0.62,
    "cutout_echo": 0.62,
    "projector": 0.72,
    "beat_flash": 0.68,
    "chromatic_hit": 0.72,
}


def effect_blend(effect: str) -> str:
    """ffmpeg/CSS-compatible blend mode for an effect."""
    return _FX_BLEND.get((effect or "").strip().lower(), "screen")


def effect_opacity(effect: str) -> float:
    """Editorial strength of the effect layer in [0, 1]."""
    return _FX_OPACITY.get((effect or "").strip().lower(), 1.0)


def is_reactive_effect(effect: str) -> bool:
    return (effect or "").strip().lower() in REACTIVE_EFFECTS


def is_generative_effect(effect: str) -> bool:
    return (effect or "").strip().lower() in GENERATIVE_EFFECTS


def is_pixel_transform(effect: str) -> bool:
    return (effect or "").strip().lower() in PIXEL_TRANSFORM_EFFECTS


@dataclass(frozen=True)
class EffectRhythm:
    """Beat grid used by audio-reactive visuals.

    `beats` are exact detector timestamps.  `strengths` are normalized
    low-frequency energies at those timestamps, so a kick can drive a
    stronger visual hit than a quiet metronomic subdivision.
    """

    bpm: float
    beats: tuple[float, ...]
    strengths: tuple[float, ...]


@lru_cache(maxsize=32)
def _detect_rhythm_cached(audio_path: str, mtime_ns: int) -> EffectRhythm:
    """Detect beat timestamps + bass energy once per source file."""
    del mtime_ns  # part of the cache key so replacing a file invalidates it
    try:
        import beat_snap
        result = beat_snap.detect_beats(audio_path)
        if result:
            bpm = float(result[0])
            beats = tuple(float(t) for t in result[1] if float(t) >= 0.0)
            if 45.0 <= bpm <= 220.0 and beats:
                strengths = _beat_strengths(audio_path, beats)
                return EffectRhythm(bpm=bpm, beats=beats, strengths=strengths)
            logger.warning(
                "[FX] invalid beat grid (bpm=%s, beats=%d); using 120 BPM",
                result[0], len(beats),
            )
    except Exception as exc:  # pragma: no cover - beat_snap is already defensive
        logger.warning("[FX] beat detection failed (%s); using 120 BPM", exc)
    return EffectRhythm(bpm=120.0, beats=(), strengths=())


def _beat_strengths(audio_path: str, beats: tuple[float, ...]) -> tuple[float, ...]:
    """Return robust 0.45..1 bass-energy weights for detected beats."""
    if not beats:
        return ()
    try:
        import librosa
        import numpy as np

        y, sr = librosa.load(audio_path, sr=11025, mono=True)
        hop = 256
        spectrum = np.abs(librosa.stft(y, n_fft=1024, hop_length=hop))
        freqs = librosa.fft_frequencies(sr=sr, n_fft=1024)
        bass = spectrum[freqs <= 180.0].mean(axis=0)
        frame_times = librosa.frames_to_time(
            np.arange(bass.size), sr=sr, hop_length=hop
        )
        values = np.array(
            [
                float(
                    bass[
                        max(0, int(np.searchsorted(frame_times, beat)) - 1):
                        min(bass.size, int(np.searchsorted(frame_times, beat)) + 2)
                    ].max(initial=0.0)
                )
                for beat in beats
            ],
            dtype=np.float64,
        )
        lo, hi = np.percentile(values, (20, 95))
        if hi <= lo + 1e-9:
            return tuple(1.0 for _ in beats)
        normalized = np.clip((values - lo) / (hi - lo), 0.0, 1.0)
        return tuple(float(0.45 + 0.55 * v) for v in normalized)
    except Exception as exc:
        logger.warning("[FX] bass-energy analysis failed (%s); using flat beats", exc)
        return tuple(1.0 for _ in beats)


def detect_effect_rhythm(effect: str, audio_path: str) -> EffectRhythm | None:
    """Full rhythm contract for a reactive effect, otherwise ``None``."""
    if not is_reactive_effect(effect) or not audio_path:
        return None
    try:
        mtime_ns = os.stat(audio_path).st_mtime_ns
    except OSError:
        return EffectRhythm(120.0, (), ())
    rhythm = _detect_rhythm_cached(os.path.abspath(audio_path), mtime_ns)
    logger.info(
        "[FX] reactive '%s': %.1f BPM, %d exact beats + bass energy",
        effect, rhythm.bpm, len(rhythm.beats),
    )
    return rhythm


def detect_effect_tempo(effect: str, audio_path: str) -> float | None:
    """Backward-compatible BPM accessor for callers outside the compositor.

    Tempo matching is intentionally independent from BEAT_SNAP_ENABLED: that
    flag controls lyric timestamp correction, while choosing a reactive visual
    explicitly opts into beat analysis.
    """
    rhythm = detect_effect_rhythm(effect, audio_path)
    return rhythm.bpm if rhythm else None


def effect_phase_trim(effect: str, beat_bpm: float | None = None,
                      beat_offset: float = 0.0) -> float:
    """Source seconds to cyclically trim for alignment to the first beat."""
    if not is_reactive_effect(effect):
        return 0.0
    try:
        bpm = float(beat_bpm or 120.0)
    except (TypeError, ValueError):
        bpm = 120.0
    bpm = min(220.0, max(45.0, bpm))
    factor = 120.0 / bpm
    # The authored loop hits at 0, .5, 1.0… source seconds. Trim into its
    # phase so the next authored hit lands on the detector's first timestamp,
    # while still emitting frames from output t=0 (no black pre-roll).
    period_out = 0.5 * factor
    offset = max(0.0, float(beat_offset or 0.0)) % period_out
    return (0.5 - (offset / factor)) % 0.5


def effect_setpts(effect: str, beat_bpm: float | None = None,
                  beat_offset: float = 0.0) -> str:
    """PTS step for an authored loop.

    Reactive assets contain one hit every 0.5 s (120 BPM). Scaling their PTS by
    120/song_BPM makes the hits recur at the detected beat interval.
    Non-reactive effects retain the historical timing exactly.
    """
    if not is_reactive_effect(effect):
        return "setpts=PTS-STARTPTS"
    try:
        bpm = float(beat_bpm or 120.0)
    except (TypeError, ValueError):
        bpm = 120.0
    bpm = min(220.0, max(45.0, bpm))
    factor = 120.0 / bpm
    source_trim = effect_phase_trim(effect, bpm, beat_offset)
    trim = f"trim=start={source_trim:.6f}," if source_trim > 0.000001 else ""
    return f"{trim}setpts=(PTS-STARTPTS)*{factor:.6f}"


def rhythm_for_window(rhythm: EffectRhythm | None, start: float,
                      duration: float | None = None) -> EffectRhythm | None:
    """Rebase a song rhythm to an exported window (short/art track)."""
    if rhythm is None:
        return None
    end = float("inf") if duration is None else start + max(0.0, duration)
    strengths = rhythm.strengths or (1.0,) * len(rhythm.beats)
    selected = [
        (beat - start, strength)
        for beat, strength in zip(rhythm.beats, strengths)
        if start <= beat <= end
    ]
    return EffectRhythm(
        rhythm.bpm,
        tuple(pair[0] for pair in selected),
        tuple(pair[1] for pair in selected),
    )


def effect_strength_at(rhythm: EffectRhythm | None, t: float,
                       decay: float = 0.16) -> float:
    """Python/moviepy equivalent of the ffmpeg beat envelope."""
    if not rhythm or not rhythm.beats:
        return 1.0
    value = 0.10
    for beat, strength in zip(
        rhythm.beats, rhythm.strengths or (1.0,) * len(rhythm.beats)
    ):
        distance = abs(float(t) - beat)
        if distance < decay:
            value += strength * (1.0 - distance / decay)
    return min(1.0, value)


def _rhythm_envelope(rhythm: EffectRhythm | None, decay: float = 0.16) -> str:
    """ffmpeg expression that pulses on exact, energy-weighted beat times."""
    if not rhythm or not rhythm.beats:
        return "1"
    # Cap pathological grids to keep filter arguments well below OS argv
    # limits. Preserve the strongest hits instead of arbitrary early beats.
    pairs = list(zip(rhythm.beats, rhythm.strengths or (1.0,) * len(rhythm.beats)))
    if len(pairs) > 900:
        keep = sorted(
            sorted(enumerate(pairs), key=lambda item: item[1][1], reverse=True)[:900],
            key=lambda item: item[0],
        )
        pairs = [item[1] for item in keep]
    pulses = [
        f"if(lt(abs(T-{beat:.4f}),{decay:.3f}),"
        f"{strength:.3f}*(1-abs(T-{beat:.4f})/{decay:.3f}),0)"
        for beat, strength in pairs
    ]
    return f"min(1,0.10+{'+'.join(pulses)})"


def rhythm_mask_graph(rhythm: EffectRhythm | None, width: int, height: int,
                      *, raw_label: str = "fxraw",
                      out_label: str = "fx") -> str:
    """Filtergraph fragment applying the exact energy-weighted beat envelope."""
    if not rhythm or not rhythm.beats:
        return f"[{raw_label}]null[{out_label}];"
    env = _rhythm_envelope(rhythm)
    return (
        f"color=c=white:s={width}x{height}:r=30,format=gray,"
        f"geq=lum='255*({env})',format=gbrp[beatmask];"
        f"[{raw_label}][beatmask]blend=all_mode=multiply:shortest=1"
        f"[{out_label}];"
    )


# palette code → ffmpeg `eq` grade. "" / "auto" / unknown → no grade
# (scene-natural). Mirrors the frontend STYLES codes used elsewhere.
_GRADE = {
    "oscuro": "eq=contrast=1.12:brightness=-0.03:saturation=1.10",
    "neon": "eq=contrast=1.10:saturation=1.35",
    "minimal": "eq=contrast=1.04:saturation=0.90",
    "calido": "eq=contrast=1.06:saturation=1.14:gamma_r=1.05:gamma_b=0.96",
}


def effect_path(effect: str) -> str | None:
    """Absolute path to the baked loop for `effect`, or None if unset/missing."""
    if not effect:
        return None
    name = effect.strip().lower()
    if name not in EFFECTS:
        return None
    p = os.path.abspath(os.path.join(_FX_DIR, f"{name}.mp4"))
    if not os.path.exists(p):
        # A KNOWN effect was requested but its baked overlay is absent on disk.
        # This silently dropped EVERY effect in prod until 2026-06-04 — the
        # Docker image didn't COPY assets/ (assets/fx lives outside backend/),
        # so effect_path() returned None and build_video_filter took the
        # no-effect path. The job still "succeeded" with no overlay. Log loudly
        # so a missing / mis-deployed asset surfaces in the worker logs instead
        # of being an invisible no-op.
        logger.warning(
            "[FX] effect '%s' requested but overlay asset is MISSING at %s — "
            "the effect will be SKIPPED in this render. Verify lyricgen/assets/fx "
            "is present in the deployed image (Dockerfile COPY assets/).",
            name, p,
        )
        return None
    return p


# Per-effect pre-blend gain. Sparse / dim effects barely register through the
# screen-blend over busy or bright photos (matrix test 2026-06-02: stars and
# bokeh were imperceptible, snow read as faint streaks). Lift the particles
# before the blend, keeping the near-black background black so the screen-blend
# doesn't haze the frame. Two shapes, validated by compositing the real assets
# over a dark+bright test bg and measuring luma:
#   - stars / snow are bright POINTS → `eq` contrast>1 (pivot 0.5) pushes the
#     bright pixels brighter and the black blacker. (stars 190→232, snow
#     224→254 brightest; dark region unchanged.)
#   - bokeh circles are MID-tone (~0.28), which an `eq` contrast would push
#     DOWN. A `curves` that lifts the 0.28 knee while pinning the low end keeps
#     the black clean and brightens the circles (67→149 over the test bg).
#   - rain / light / aurora are dim mid-tone shapes (thin streaks / diffuse
#     glow) that an `eq` contrast would also crush → `curves` that lift their
#     band (see per-entry notes below). Boosted 2026-06-04 once the assets
#     finally reached the render. Applied identically in the main libass path
# (build_video_filter) and the short post-pass (_apply_short_effect) so the
# effect looks the same in both.
_FX_GAIN = {
    "stars": "eq=contrast=2.0:brightness=-0.02",
    "snow": "eq=contrast=1.35",
    # 2026-06-04: stronger lift. The bokeh loop is very dim (mean ~7% luma) so
    # the previous mid-tone curve barely registered through the screen-blend
    # over busy/dark photos (operator: "el bokeh no salió en el render").
    # Local compositing test on dark/busy/light backgrounds: pushing the
    # circles' mid-tones (0.16-0.28) to near-white (0.85-1.0) while keeping the
    # near-black loop background black makes them POP on dark+busy (dark bg luma
    # 27→40 vs 27→31 before) without blowing highlights. Light backgrounds stay
    # washed — a screen-blend math limit, not the curve; needs a brightness-
    # adaptive blend (follow-up), but the render darkens backgrounds anyway.
    "bokeh": "curves=all='0/0 0.07/0.02 0.16/0.85 0.28/1 1/1'",
    # 2026-06-04: rain / light / aurora boosted (operator: "boostea los 3").
    # These previously had NO gain ("read fine"), but once the assets actually
    # reached the render (see fx_compositor._FX_DIR fix) they were noticeably
    # subtle vs bokeh/snow. Tuned by local compositing over a dusk bg + luma
    # measurement, same method as bokeh:
    #   - rain: thin bright streaks (raw YMAX ~200) but many mid-tone ones an
    #     `eq` contrast would CRUSH (pivot 0.5 pushes <0.5 down → tested:
    #     composite YMAX 139→129, worse). A `curves` that LIFTS the streak
    #     band (0.3→0.65, 0.6→1) while pinning the near-black bg low brings
    #     composite YMAX 139→229 with YAVG unchanged (bg stays dark).
    #   - light / aurora / prism: broad diffuse glows. Contrast would dim them
    #     because most pixels sit below the 0.5 pivot, so lift their mid band.
    "rain": "curves=all='0/0 0.1/0.03 0.3/0.65 0.6/1 1/1'",
    "light": "curves=all='0/0 0.06/0.015 0.2/0.55 0.31/0.92 1/1'",
    "aurora": "curves=all='0/0 0.06/0.015 0.2/0.55 0.31/0.92 1/1'",
    "prism": "curves=all='0/0 0.06/0.015 0.2/0.52 0.35/0.9 1/1'",
    "dust": "eq=contrast=1.55:brightness=-0.015",
    "embers": "curves=all='0/0 0.08/0.025 0.25/0.65 0.55/1 1/1'",
    "petals": "eq=contrast=1.20",
    "film": "curves=all='0/0 0.05/0.02 0.18/0.48 0.5/0.88 1/1'",
    "scanlines": "curves=all='0/0 0.05/0.025 0.18/0.52 0.5/0.9 1/1'",
    "fog": "curves=all='0/0 0.06/0.025 0.20/0.62 0.5/0.95 1/1'",
    "shapes": "curves=all='0/0 0.08/0.025 0.28/0.72 0.60/1 1/1'",
    # The source uses large deterministic leaf-like masks. Blur at composite
    # resolution turns their hard procedural edges into natural soft shadows.
    "shadow_play": "gblur=sigma=18",
}


def fx_gain(effect: str) -> str:
    """ffmpeg pre-filter for an effect before blend, or ''.

    Usually a gain curve; some treatments use a blur. Goes BEFORE
    `format=gbrp` in the fx chain; empty for effects that need no treatment."""
    return _FX_GAIN.get((effect or "").strip().lower(), "")


def _parse_custom_colors(custom_colors: str) -> list[tuple[int, int, int]]:
    """Parse '#RRGGBB,#RRGGBB' (o '#RGB' shorthand) → lista de tuplas RGB.
    Robusto: ignora entradas inválidas en vez de raisear."""
    if not custom_colors:
        return []
    out: list[tuple[int, int, int]] = []
    for token in custom_colors.split(","):
        c = token.strip().lstrip("#")
        if len(c) == 3:
            c = "".join(ch * 2 for ch in c)
        if len(c) != 6:
            continue
        try:
            r = int(c[0:2], 16)
            g = int(c[2:4], 16)
            b = int(c[4:6], 16)
            out.append((r, g, b))
        except ValueError:
            continue
    return out


def _custom_grade_params(rgbs: list[tuple[int, int, int]]) -> tuple[float, float, float, float, float]:
    """Deriva (contrast, sat, gamma_r, gamma_g, gamma_b) desde RGB promedio.
    Estrategia: gamma-por-canal empuja midtones hacia el avg color del
    operador; bump contrast + saturation fijos (custom = paleta vivid)."""
    avg_r = sum(r for r, _, _ in rgbs) / len(rgbs)
    avg_g = sum(g for _, g, _ in rgbs) / len(rgbs)
    avg_b = sum(b for _, _, b in rgbs) / len(rgbs)

    def gamma_for(channel: float) -> float:
        if channel < 32.0:
            return 1.20
        return max(0.85, min(1.20, 128.0 / channel))

    return (1.08, 1.20, gamma_for(avg_r), gamma_for(avg_g), gamma_for(avg_b))


def grade_filter(style: str, custom_colors: str = "") -> str:
    """ffmpeg `eq` grade string for the palette, or '' for auto/none.

    U3 (audit 2026-05-25): custom_colors AHORA se aplica en el grade.
    Antes era "deferred to later phase" — la fase nunca llegó. El operador
    elegía paleta custom, Veo respetaba los colores en el prompt, pero
    el grade final ignoraba la selección → tono inconsistente.
    Strategy: gamma-per-channel desde RGB promedio, bump contrast + sat.
    No LUT (overkill); eq cubre el 80% del caso con 20% del effort.
    """
    style_key = (style or "").strip().lower()
    if style_key == "custom":
        rgbs = _parse_custom_colors(custom_colors)
        if rgbs:
            c, s, gr, gg, gb = _custom_grade_params(rgbs)
            return f"eq=contrast={c}:saturation={s}:gamma_r={gr:.2f}:gamma_g={gg:.2f}:gamma_b={gb:.2f}"
    return _GRADE.get(style_key, "")


# Numpy equivalent of the `eq` presets for the moviepy render path (which can't
# use the ffmpeg eq filter). (contrast, saturation, brightness_delta_0_255).
# Kept alongside _GRADE so both render paths apply the SAME palette grade.
_GRADE_NUMPY = {
    "oscuro": (1.12, 1.10, -8.0),
    "neon": (1.10, 1.35, 0.0),
    "minimal": (1.04, 0.90, 0.0),
    "calido": (1.06, 1.14, 0.0),
}


def grade_frame(frame, style: str, custom_colors: str = ""):
    """Apply the palette grade (contrast/saturation/brightness) to a float RGB
    numpy frame, for the moviepy path. Returns the frame unchanged for auto/
    unknown palettes. Mirrors `grade_filter`'s ffmpeg `eq` as closely as numpy
    allows (not bit-identical, same intent).

    U3 (audit 2026-05-25): custom_colors aplica gamma-per-channel + contrast/
    sat bump (parity con ffmpeg path)."""
    import numpy as np
    style_key = (style or "").strip().lower()

    if style_key == "custom":
        rgbs = _parse_custom_colors(custom_colors)
        if rgbs:
            contrast, sat, gr, gg, gb = _custom_grade_params(rgbs)
            f = (frame - 127.5) * contrast + 127.5
            f = np.clip(f, 0.0, 255.0) / 255.0
            f[:, :, 0] = np.power(f[:, :, 0], gr)
            f[:, :, 1] = np.power(f[:, :, 1], gg)
            f[:, :, 2] = np.power(f[:, :, 2], gb)
            f = f * 255.0
            luma = (0.299 * f[:, :, 0] + 0.587 * f[:, :, 1] + 0.114 * f[:, :, 2])
            f = luma[:, :, None] + (f - luma[:, :, None]) * sat
            return f

    p = _GRADE_NUMPY.get(style_key)
    if not p:
        return frame
    contrast, sat, bright = p
    f = (frame - 127.5) * contrast + 127.5 + bright
    luma = (0.299 * f[:, :, 0] + 0.587 * f[:, :, 1] + 0.114 * f[:, :, 2])
    f = luma[:, :, None] + (f - luma[:, :, None]) * sat
    return f


def transform_photo_frame(frame, effect: str, t: float, fx_frame=None):
    """Moviepy fallback for the photo-derived production treatments.

    FFmpeg remains the primary renderer. This bounded numpy/Pillow equivalent
    prevents a worker that falls back to moviepy from silently degrading a
    selected transform into a plain decorative overlay.
    """
    import numpy as np
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps

    effect = (effect or "").strip().lower()
    base = np.clip(frame, 0, 255).astype(np.uint8)
    height, width = base.shape[:2]

    def screen(a, b):
        return 255.0 - (255.0 - a) * (255.0 - b) / 255.0

    def resize_center(source, ratio):
        target_w = max(2, int(width * ratio))
        target_h = max(2, int(height * ratio))
        resized = np.asarray(
            Image.fromarray(source).resize((target_w, target_h), Image.Resampling.BILINEAR)
        )
        canvas = np.zeros_like(source)
        x, y = (width - target_w) // 2, (height - target_h) // 2
        canvas[y:y + target_h, x:x + target_w] = resized
        return canvas

    out = base.astype(np.float32)
    if effect in {"liquid_glass", "heatwave"}:
        amplitude = 15 if effect == "liquid_glass" else 11
        frequency = 1.0 if effect == "liquid_glass" else 5.0
        speed = 0.22 if effect == "liquid_glass" else -0.45
        warped = np.empty_like(base)
        for y in range(height):
            shift = int(
                amplitude * np.sin(2 * np.pi * (y / height * frequency + t * speed))
            )
            warped[y] = np.roll(base[y], shift, axis=0)
        out = warped.astype(np.float32)
    elif effect in {"rgb_glitch", "chromatic_pulse"}:
        shift = 14
        if effect == "chromatic_pulse":
            breathe = .5 - .5 * np.cos(2 * np.pi * float(t) / 4.0)
            shift = max(1, int(2 + 4 * breathe))
        out = base.astype(np.float32)
        out[:, :, 0] = np.roll(base[:, :, 0], shift, axis=1)
        out[:, :, 2] = np.roll(base[:, :, 2], -shift, axis=1)
    elif effect == "neon_edge":
        edge = ImageOps.grayscale(
            Image.fromarray(base).filter(ImageFilter.FIND_EDGES)
        )
        edge_arr = np.asarray(edge, dtype=np.float32)
        edge_color = np.stack(
            (edge_arr * .42, edge_arr * .82, edge_arr),
            axis=2,
        )
        out = screen(base.astype(np.float32) * 0.94, edge_color * 0.58)
    elif effect == "kaleido":
        half_h, half_w = max(1, height // 2), max(1, width // 2)
        y0, x0 = max(0, (height - half_h) // 2), max(0, (width - half_w) // 2)
        quadrant = base[y0:y0 + half_h, x0:x0 + half_w]
        top = np.concatenate([quadrant, np.flip(quadrant, axis=1)], axis=1)
        tiled = np.concatenate([top, np.flip(top, axis=0)], axis=0)
        out = np.asarray(
            Image.fromarray(tiled).resize((width, height), Image.Resampling.BILINEAR),
            dtype=np.float32,
        )
    elif effect == "halftone":
        tiny = Image.fromarray(base).resize(
            (max(8, width // 10), max(8, height // 10)), Image.Resampling.BOX
        )
        out = np.asarray(
            tiny.resize((width, height), Image.Resampling.NEAREST),
            dtype=np.float32,
        )
    elif effect == "ink_reveal":
        ink_alt_image = ImageEnhance.Contrast(
            ImageOps.grayscale(Image.fromarray(base)).convert("RGB")
        ).enhance(1.32)
        ink_alt = np.asarray(
            ImageEnhance.Brightness(ink_alt_image).enhance(.66),
            dtype=np.float32,
        )
        if fx_frame is not None:
            fx_gray = np.asarray(fx_frame, dtype=np.float32).mean(axis=2)
            mask = np.clip(1.0 - fx_gray / 255.0, 0.0, 1.0)
        else:
            yy, xx = np.indices((height, width), dtype=np.float32)
            progress = .5 - .5 * np.cos(2 * np.pi * float(t) / 8.0)
            center = height * (.34 + .10 * np.sin(
                2 * np.pi * (xx / max(1, width) * 1.05 + .08)
            ))
            brush = np.clip(
                (height * .065 - np.abs(yy - center)) / max(1.0, height * .02),
                0.0,
                1.0,
            )
            reveal = np.clip(
                (progress * width * 1.2 - xx) / max(1.0, width * .08),
                0.0,
                1.0,
            )
            mask = brush * reveal
        out = base.astype(np.float32) * (1.0 - mask[:, :, None]) + ink_alt * mask[:, :, None]
        if fx_frame is not None:
            # Preserve a restrained amount of the authored dry-brush texture.
            # The previous circular asset made this look like dirty-lens blobs;
            # the corrected mask contains real bristles and bounded splatter.
            texture = np.asarray(fx_frame, dtype=np.float32).mean(axis=2) / 255.0
            out *= (.88 + .12 * texture[:, :, None])
    elif effect == "cutout_echo":
        echo1 = resize_center(base, 0.94).astype(np.float32)
        echo2 = resize_center(base, 0.88).astype(np.float32)
        out = screen(base.astype(np.float32), echo1 * 0.22)
        out = screen(out, echo2 * 0.15)
    elif effect == "projector":
        angle = 0.45 * np.sin(2 * np.pi * float(t) / 3.7)
        gate = Image.fromarray(base).rotate(
            angle, resample=Image.Resampling.BILINEAR, expand=False
        )
        out = np.asarray(
            ImageEnhance.Contrast(gate).enhance(1.08), dtype=np.float32
        ) * 0.90
    elif effect == "foto_viva":
        # Deterministic fallback for the generative image-to-video path:
        # subtly move only the soft region supplied by the procedural mask.
        # The mask travels across the photo, so unlike the old light overlay it
        # is not nailed to one position for the whole song.
        # Eight percent gives the local fallback enough travel to read after
        # H.264 compression while remaining a bounded subject motion rather
        # than turning into a global Ken Burns zoom.
        scale = 1.08
        scaled_w, scaled_h = max(width + 2, int(width * scale)), max(height + 2, int(height * scale))
        enlarged = np.asarray(
            Image.fromarray(base).resize(
                (scaled_w, scaled_h), Image.Resampling.BICUBIC
            )
        )
        room_x, room_y = scaled_w - width, scaled_h - height
        x0 = int((room_x / 2) * (1.0 + np.sin(2 * np.pi * float(t) / 8.0)))
        y0 = int((room_y / 2) * (1.0 + np.cos(2 * np.pi * float(t) / 8.0)))
        moved = enlarged[y0:y0 + height, x0:x0 + width].astype(np.float32)
        if fx_frame is not None:
            mask = np.asarray(fx_frame, dtype=np.float32).mean(axis=2) / 255.0
        else:
            yy, xx = np.indices((height, width), dtype=np.float32)
            cx = width * (0.5 + 0.24 * np.sin(2 * np.pi * float(t) / 8.0))
            cy = height * (0.5 + 0.18 * np.cos(2 * np.pi * float(t) / 8.0))
            mask = np.exp(
                -(((xx - cx) / max(1.0, width * 0.24)) ** 2
                  + ((yy - cy) / max(1.0, height * 0.32)) ** 2)
            )
        mask = np.clip(mask, 0.0, 1.0)[:, :, None] * 0.88
        out = base.astype(np.float32) * (1.0 - mask) + moved * mask

    # Ink uses the loop as its actual merge mask above. Other transform loops
    # remain restrained auxiliary light/dot layers, matching the FFmpeg graph.
    if fx_frame is not None and effect not in {"ink_reveal", "foto_viva"}:
        layer = np.clip(fx_frame, 0, 255).astype(np.float32)
        opacity = {
            "liquid_glass": 0.34, "heatwave": 0.26, "rgb_glitch": 0.18,
            "neon_edge": 0.10, "kaleido": 0.03, "halftone": 0.36,
            "chromatic_pulse": 0.22, "cutout_echo": 0.24, "projector": 0.42,
        }.get(effect, effect_opacity(effect))
        mixed = (
            out * layer / 255.0
            if effect_blend(effect) == "multiply"
            else screen(out, layer)
        )
        out = out * (1.0 - opacity) + mixed * opacity
    return np.clip(out, 0, 255)


def _escape_filter_path(p: str) -> str:
    """Escape a path for use as an ffmpeg filter option value (subtitles/
    fontsdir). Backslash first, then the filtergraph delimiters."""
    return p.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _overlay_graph(*, blend: str, opacity: float,
                   bg: str = "bg", fx: str = "fx", out: str = "bl") -> str:
    opacity_step = f":all_opacity={opacity:.2f}" if opacity < 1.0 else ""
    return (
        f"[{bg}][{fx}]blend=all_mode={blend}{opacity_step}:shortest=1"
        f"[{out}]"
    )


def _pixel_transform_graph(effect: str, width: int, height: int) -> str:
    """Photo-derived compositor graph from prepared ``[bg]`` + ``[fx]``.

    Each branch must end in ``[bl]`` and remain deterministic/portable across
    the ffmpeg builds used locally and in the worker image.
    """
    effect = (effect or "").strip().lower()
    half_w, half_h = max(2, width // 2), max(2, height // 2)
    inset_w, inset_h = max(2, width - 14), height
    echo1_w, echo1_h = max(2, int(width * 0.94)), max(2, int(height * 0.94))
    echo2_w, echo2_h = max(2, int(width * 0.88)), max(2, int(height * 0.88))
    live_w, live_h = max(width + 2, int(width * 1.08)), max(height + 2, int(height * 1.08))
    pulse_shift = max(3, int(width * .006))
    pulse_w = max(2, width - pulse_shift)

    if effect == "liquid_glass":
        xmap = (
            "128+15*sin(2*PI*(Y/H+T*0.22))"
            "+5*sin(2*PI*(X/W*2-T*0.10))"
        )
        ymap = "128+7*cos(2*PI*(X/W+T*0.15))"
        return (
            "[bg]split=3[liqsrc][liqx][liqy];"
            f"[liqx]geq=r='{xmap}':g='{xmap}':b='{xmap}'[liqxm];"
            f"[liqy]geq=r='{ymap}':g='{ymap}':b='{ymap}'[liqym];"
            "[liqsrc][liqxm][liqym]displace=edge=mirror[liq];"
            "[liq][fx]blend=all_mode=screen:all_opacity=0.34:shortest=1[bl]"
        )
    if effect == "heatwave":
        xmap = "128+11*sin(2*PI*(Y/H*5-T*0.45))"
        ymap = "128+3*cos(2*PI*(X/W*2+T*0.20))"
        return (
            "[bg]split=3[heatsrc][heatx][heaty];"
            f"[heatx]geq=r='{xmap}':g='{xmap}':b='{xmap}'[heatxm];"
            f"[heaty]geq=r='{ymap}':g='{ymap}':b='{ymap}'[heatym];"
            "[heatsrc][heatxm][heatym]displace=edge=mirror[warped];"
            "[warped][fx]blend=all_mode=screen:all_opacity=0.26:shortest=1[bl]"
        )
    if effect == "rgb_glitch":
        return (
            "[bg]split=3[glbase][glr][glc];"
            f"[glr]crop={inset_w}:{inset_h}:0:0,"
            f"pad={width}:{height}:14:0:black,"
            "colorchannelmixer=gg=0:bb=0[redshift];"
            f"[glc]crop={inset_w}:{inset_h}:14:0,"
            f"pad={width}:{height}:0:0:black,"
            "colorchannelmixer=rr=0[cyanshift];"
            "[glbase][redshift]blend=all_mode=screen:all_opacity=0.12[gla];"
            "[gla][cyanshift]blend=all_mode=screen:all_opacity=0.10[glb];"
            "[glb][fx]blend=all_mode=screen:all_opacity=0.18:shortest=1[bl]"
        )
    if effect == "neon_edge":
        return (
            "[bg]split=2[neonbase][neonsrc];"
            "[neonbase]curves=all='0/0 0.5/0.46 1/0.96'[neondim];"
            "[neonsrc]edgedetect=mode=wires:low=0.06:high=0.18,"
            "curves=all='0/0 0.08/0 0.26/0.88 1/1',"
            "colorchannelmixer=rr=0.42:gg=0.82:bb=1.0[edges];"
            "[neondim][edges]blend=all_mode=screen:all_opacity=0.58[neon];"
            "[neon][fx]blend=all_mode=screen:all_opacity=0.10:shortest=1[bl]"
        )
    if effect == "kaleido":
        return (
            "[bg]rotate='0.022*sin(2*PI*t/6)':ow=iw:oh=ih:fillcolor=black,"
            f"crop={half_w}:{half_h}:(in_w-out_w)/2:(in_h-out_h)/2,"
            "split=4[k1][k2i][k3i][k4i];"
            "[k2i]hflip[k2];[k3i]vflip[k3];[k4i]hflip,vflip[k4];"
            "[k1][k2]hstack=inputs=2[ktop];"
            "[k3][k4]hstack=inputs=2[kbottom];"
            "[ktop][kbottom]vstack=inputs=2[kphoto];"
            f"[kphoto]scale={width}:{height}[kscaled];"
            "[kscaled][fx]blend=all_mode=screen:all_opacity=0.03:shortest=1[bl]"
        )
    if effect == "halftone":
        dot_w, dot_h = max(8, width // 10), max(8, height // 10)
        return (
            f"[bg]scale={dot_w}:{dot_h}:flags=area,"
            f"scale={width}:{height}:flags=neighbor[poster];"
            "[poster][fx]blend=all_mode=multiply:all_opacity=0.36:shortest=1[bl]"
        )
    if effect == "ink_reveal":
        return (
            "[bg]split=2[inkbase][inkalt];"
            "[inkalt]eq=saturation=0.08:contrast=1.40:brightness=-0.090[inkwash];"
            "[fx]split=2[inkmasksrc][inktexture];"
            "[inkmasksrc]format=gray,negate,gblur=sigma=1.4,"
            "curves=all='0/0 0.18/0.03 0.58/0.90 1/1'[inkmask];"
            "[inkbase][inkwash][inkmask]maskedmerge[inkmerged];"
            "[inkmerged][inktexture]blend=all_mode=multiply:"
            "all_opacity=0.12:shortest=1[bl]"
        )
    if effect == "chromatic_pulse":
        return (
            "[bg]split=5[cpbase][cprbase][cprmovei][cpbbase][cpbmovei];"
            f"[cprmovei]crop={pulse_w}:{height}:0:0,"
            f"pad={width}:{height}:{pulse_shift}:0:black[cprmove];"
            "[cprbase][cprmove]blend=all_mode=difference,"
            "eq=contrast=1.65:brightness=-0.022,"
            "colorchannelmixer=rr=1.35:gg=0:bb=0[cpred];"
            f"[cpbmovei]crop={pulse_w}:{height}:{pulse_shift}:0,"
            f"pad={width}:{height}:0:0:black[cpbmove];"
            "[cpbbase][cpbmove]blend=all_mode=difference,"
            "eq=contrast=1.65:brightness=-0.022,"
            "colorchannelmixer=rr=0:gg=0:bb=1.35[cpblue];"
            "[cpbase][cpred]blend=all_mode=screen:all_opacity=0.48[cpa];"
            "[cpa][cpblue]blend=all_mode=screen:all_opacity=0.44[cpb2];"
            "[cpb2][fx]blend=all_mode=screen:all_opacity=0.22:shortest=1[bl]"
        )
    if effect == "cutout_echo":
        return (
            "[bg]split=3[echobase][echo1i][echo2i];"
            f"[echo1i]scale={echo1_w}:{echo1_h},"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,"
            "eq=saturation=1.25[echo1];"
            f"[echo2i]scale={echo2_w}:{echo2_h},"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,"
            "eq=saturation=0.75[echo2];"
            "[echobase][echo1]blend=all_mode=screen:all_opacity=0.22[echoa];"
            "[echoa][echo2]blend=all_mode=screen:all_opacity=0.15[echob];"
            "[echob][fx]blend=all_mode=screen:all_opacity=0.24:shortest=1[bl]"
        )
    if effect == "projector":
        return (
            "[bg]rotate='0.008*sin(2*PI*t/3.7)':ow=iw:oh=ih:fillcolor=black,"
            "eq=contrast=1.08:brightness=-0.025:saturation=0.88,"
            "vignette=PI/5.2:eval=frame[gate];"
            "[gate][fx]blend=all_mode=screen:all_opacity=0.42:shortest=1[bl]"
        )
    if effect == "foto_viva":
        room_x, room_y = live_w - width, live_h - height
        return (
            "[bg]split=2[livebase][livesrc];"
            f"[livesrc]scale={live_w}:{live_h}:flags=lanczos,"
            f"crop={width}:{height}:"
            f"x='{room_x}/2*(1+sin(2*PI*t/8))':"
            f"y='{room_y}/2*(1+cos(2*PI*t/8))'[livemove];"
            "[fx]format=gray,curves=all='0/0 0.18/0 0.72/1 1/1'[livemask];"
            "[livebase][livemove][livemask]maskedmerge[bl]"
        )
    raise ValueError(f"Unsupported pixel transform: {effect}")


def build_video_filter(*, ass_basename: str | None, font_dir: str, width: int,
                       height: int, effect: str = "", style: str = "",
                       custom_colors: str = "",
                       beat_bpm: float | None = None,
                       rhythm: EffectRhythm | None = None,
                       fx_input_index: int = 2):
    """Build the video filter for the single-pass libass render.

    Returns (filter_str, use_complex, extra_inputs):
      - No effect → ('<grade>,subtitles=…' , False, [])
            caller uses:  -vf <filter_str> -map 0:v -map 1:a
      - Effect    → ('<filter_complex>', True, ['-stream_loop','-1','-i',<fx>])
            caller uses:  -filter_complex <filter_str> -map [out] -map 1:a

    Input index contract: bg=0, audio=1, fx=2 (the extra_inputs are appended
    AFTER the bg and audio inputs in the ffmpeg command).

    Art tracks pass `ass_basename=None` to skip the subtitle burn entirely
    (no lyrics rendered) — the background already carries the full
    composition (blurred cover fill + centered cover). The effect overlay
    and color grade still compose the same way; `null` keeps the filter
    string valid when neither subs nor grade apply.
    """
    subs = (f"subtitles={ass_basename}:fontsdir={_escape_filter_path(font_dir)}"
            if ass_basename else "")
    grade = grade_filter(style, custom_colors)
    fx = effect_path(effect)

    if not fx:
        # No effect: keep the original cheap -vf path (optionally graded).
        steps = [s for s in (grade, subs) if s]
        vf = ",".join(steps) if steps else "null"
        return vf, False, []

    grade_step = f"{grade}," if grade else ""
    subs_step = subs if subs else "null"
    gain = fx_gain(effect)
    gain_step = f"{gain}," if gain else ""  # before format=gbrp (eq on native YUV)
    bpm = rhythm.bpm if rhythm else beat_bpm
    first_beat = rhythm.beats[0] if rhythm and rhythm.beats else 0.0
    timing = effect_setpts(effect, bpm, first_beat)
    blend = effect_blend(effect)
    opacity = effect_opacity(effect)
    prep = (
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},format=gbrp[bg];"
        f"[{fx_input_index}:v]scale={width}:{height},{timing},"
        f"{gain_step}format=gbrp[fxraw];"
    )
    prep += rhythm_mask_graph(rhythm, width, height)

    composite = (
        _pixel_transform_graph(effect, width, height)
        if is_pixel_transform(effect)
        else _overlay_graph(blend=blend, opacity=opacity)
    )
    fc = (
        f"{prep}{composite};"
        f"[bl]{grade_step}format=yuv420p[gr];"
        f"[gr]{subs_step}[out]"
    )
    return fc, True, ["-stream_loop", "-1", "-i", fx]
