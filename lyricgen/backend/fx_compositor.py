"""Effect-overlay + color-grade compositing for the single-pass lyric render.

The render burns lyrics with libass in ONE ffmpeg pass (see pipeline.py
`_render_lyrics_ass`). This module builds the video filter for that pass,
optionally adding two layers BEFORE the subtitles burn:

  1. EFFECT overlay (snow / rain / stars / bokeh / light): a pre-baked,
     seamless, black-background RGB loop at assets/fx/<effect>.mp4 (built once
     by scripts/gen_fx_loops.py), composited with `blend=all_mode=screen`.
     Effects are ADDITIVE (light-emitting), so screen-blend over black needs no
     alpha channel — plain H.264, fast to decode.

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
import os

_FX_DIR = os.path.join(os.path.dirname(__file__), "..", "assets", "fx")

# Available pre-baked effect loops (must match scripts/gen_fx_loops.py).
# 2026-05-25: "aurora" agregado para cubrir el estilo UMG observado en
# los refs Boza / Yatra Cristina / A los cuatro vientos — líneas
# brillantes ondulantes que flotan sobre el cielo. Asset placeholder
# basado en light.mp4 hasta que se swap el real (loop ondas + glow).
EFFECTS = ("snow", "rain", "stars", "bokeh", "light", "aurora")

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
    return p if os.path.exists(p) else None


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
# rain / light already read fine → no gain. aurora is a broad diffuse glow that
# no tested gain improved cleanly → left untouched. Applied identically in the
# main libass path (build_video_filter) and the short post-pass
# (_apply_short_effect) so the effect looks the same in both.
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
}


def fx_gain(effect: str) -> str:
    """ffmpeg `eq` to pre-amplify a dim effect before screen-blend, or ''.

    Goes BEFORE `format=gbrp` in the fx chain (eq runs on the clip's native
    YUV); empty string for effects that are already bright enough."""
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


def _escape_filter_path(p: str) -> str:
    """Escape a path for use as an ffmpeg filter option value (subtitles/
    fontsdir). Backslash first, then the filtergraph delimiters."""
    return p.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def build_video_filter(*, ass_basename: str, font_dir: str, width: int,
                       height: int, effect: str = "", style: str = "",
                       custom_colors: str = ""):
    """Build the video filter for the single-pass libass render.

    Returns (filter_str, use_complex, extra_inputs):
      - No effect → ('<grade>,subtitles=…' , False, [])
            caller uses:  -vf <filter_str> -map 0:v -map 1:a
      - Effect    → ('<filter_complex>', True, ['-stream_loop','-1','-i',<fx>])
            caller uses:  -filter_complex <filter_str> -map [out] -map 1:a

    Input index contract: bg=0, audio=1, fx=2 (the extra_inputs are appended
    AFTER the bg and audio inputs in the ffmpeg command).
    """
    subs = f"subtitles={ass_basename}:fontsdir={_escape_filter_path(font_dir)}"
    grade = grade_filter(style, custom_colors)
    fx = effect_path(effect)

    if not fx:
        # No effect: keep the original cheap -vf path (optionally graded).
        vf = f"{grade},{subs}" if grade else subs
        return vf, False, []

    grade_step = f"{grade}," if grade else ""
    gain = fx_gain(effect)
    gain_step = f"{gain}," if gain else ""  # before format=gbrp (eq on native YUV)
    fc = (
        f"[0:v]format=gbrp[bg];"
        f"[2:v]scale={width}:{height},setpts=PTS-STARTPTS,{gain_step}format=gbrp[fx];"
        f"[bg][fx]blend=all_mode=screen:shortest=1[bl];"
        f"[bl]{grade_step}format=yuv420p[gr];"
        f"[gr]{subs}[out]"
    )
    return fc, True, ["-stream_loop", "-1", "-i", fx]
