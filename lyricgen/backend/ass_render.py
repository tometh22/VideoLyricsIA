"""ASS (Advanced SubStation Alpha) subtitle generation for the fast
lyric-render path.

WHY THIS EXISTS
---------------
The legacy text path (pipeline._make_text_clip + CompositeVideoClip)
renders every frame in Python via moviepy: a 3-min video at 24fps is
~4,300 frames, each compositing the background plus N text layers
(rasterized through ImageMagick). That Python frame loop is the
dominant cost of a re-render (~8-15 min on staging).

libass renders the exact same overlay (white fill + black outline +
shadow + fades + per-line sizing) inside a single ffmpeg pass, in C.
A 3-min video drops to ~30-60s. This module turns the timed lyric
segments into an ASS file; the ffmpeg `subtitles` filter burns it onto
the (already ffmpeg-looped) background.

PARITY CONTRACT
---------------
The *styling decisions* (fontsize tiers by text length, fade durations,
the perceptual fade offset, contrast outline/shadow widths) stay in
pipeline.py as the single source of truth. This module is a pure
formatter: callers pass already-computed values per line and we emit
valid ASS. That keeps the look identical to the moviepy path and makes
this module trivially unit-testable (no moviepy / ImageMagick import).

ASS reference notes:
  - Colours are &HAABBGGRR (alpha first, then blue/green/red). Alpha 00
    is fully opaque, FF fully transparent. White=&H00FFFFFF,
    black=&H00000000.
  - Alignment uses the numpad layout: 5 = middle-center (matches the
    legacy lyrics, which are vertically+horizontally centered;
    base_y = (height - text_h)//2 in _make_text_clip).
  - Timestamps are H:MM:SS.cs (centiseconds, two digits).
  - \\fad(in_ms,out_ms) is the per-line fade; omit for hard cuts.
  - \\fs<n> overrides the per-line font size (legacy sizes lines by
    character count, so size varies line to line).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AssLine:
    """One timed lyric line, with its already-computed style values.

    start_s / end_s already include any perceptual fade offset the
    caller wants (we do not re-derive it here — parity logic lives in
    pipeline). text is the final display string (case transform already
    applied)."""
    text: str
    start_s: float
    end_s: float
    fontsize: int
    fade_in_ms: int = 0
    fade_out_ms: int = 0


# --- Style tiers (single source of truth, mirrored by pipeline) ---------
#
# These mirror the legacy sizing/fade logic in pipeline._make_text_clip
# exactly. Keeping them here (pure, testable) lets BOTH the moviepy path
# and the ASS path compute identical values, so swapping engines never
# shifts the look. pipeline._make_text_clip is refactored to call these.

_FADE_DURATIONS_S = {"fade": 0.15, "fade_slow": 0.30}


def lyric_fontsize(text_len: int, scale: float, font_scale: float = 1.0) -> int:
    """Font size for a lyric line, by character count (legacy tiers).

    Mirrors _make_text_clip: >80 chars → 55, >50 → 70, else 85 (each
    multiplied by spec.text_scale), then the user's font_scale (clamped
    0.6-1.5), floored at 18px."""
    font_scale = max(0.6, min(1.5, float(font_scale or 1.0)))
    if text_len > 80:
        base = 55
    elif text_len > 50:
        base = 70
    else:
        base = 85
    base_fontsize = int(round(base * scale))
    return max(18, int(round(base_fontsize * font_scale)))


def fade_seconds(lyric_transition: str, seg_duration: float) -> float:
    """Fade in/out duration in seconds, capped at 1/3 of the segment so
    short lines don't fully dissolve. Cuts → 0."""
    fade = _FADE_DURATIONS_S.get(lyric_transition, 0.0)
    return min(fade, max(0.0, seg_duration) / 3)


def perceptual_start(seg_start: float, fade_dur: float) -> float:
    """Shift the visual onset earlier by half the fade so the perceived
    'appear' moment (~50% opacity) lands on the operator's anchored
    timestamp. Mirrors _make_text_clip's fade_perceptual_offset."""
    return max(0.0, seg_start - fade_dur / 2.0)


def font_family(font_path: str) -> tuple[str, bool]:
    """Resolve a .ttf path to its libass-matchable (family_name, is_bold).

    libass matches fonts by family NAME + weight, not by file path — so
    the moviepy path (which hands ImageMagick the file directly) and the
    ASS path must agree on the family name or libass silently falls back
    to a default font (visible parity break).

    We read the font's own name table via Pillow (already a dependency).
    is_bold is derived from the file's actual style/family string (not
    forced) so libass requests the weight the file really is: e.g.
    Oswald-Bold → ("Oswald", True); Anton-Regular → ("Anton", False),
    which avoids libass synthesising fake-bold on a display face that is
    already heavy by design.

    Pair this with a single-font fontsdir (see single_font_dir) so libass
    has exactly one candidate and can't mis-match across the pool.
    """
    from PIL import ImageFont
    f = ImageFont.truetype(font_path, size=16)
    family, style = f.getname()
    blob = f"{family} {style}".lower()
    is_bold = any(w in blob for w in ("bold", "black", "heavy"))
    return family, is_bold


def single_font_dir(font_path: str) -> str:
    """Copy one font into a fresh temp dir and return it, so the ffmpeg
    `subtitles` filter can point `fontsdir` at a directory with exactly
    one font — removing any family-matching ambiguity in libass.

    Caller owns cleanup of the returned dir."""
    import os
    import shutil
    import tempfile
    d = tempfile.mkdtemp(prefix="ass_fonts_")
    shutil.copy2(font_path, os.path.join(d, os.path.basename(font_path)))
    return d


def _clean_display_text(text: str, case_fn) -> str:
    """Apply the case transform + the same sanitisation _make_text_clip
    does before handing text to the renderer (NFC normalise, drop chars
    that break the text engine). Returns "" for blank lines so the
    caller can skip them."""
    import unicodedata
    cased = case_fn(text) if case_fn else text
    out = unicodedata.normalize("NFC", cased)
    out = out.replace("@", "").replace("`", "'").replace("\x00", "")
    return out if out.strip() else ""


def segments_to_lines(
    segments: list[dict],
    *,
    text_scale: float,
    font_scale: float = 1.0,
    lyric_transition: str = "cut",
    case_fn=None,
) -> list[AssLine]:
    """Convert segments_json into ASS lines with parity to the moviepy
    path: same case/sanitise, same fontsize tiers (by cleaned-text
    length), same fade durations + perceptual onset offset.

    case_fn: optional callable applied to each line's raw text (pipeline
    passes _apply_case; tests pass None for identity)."""
    lines: list[AssLine] = []
    for seg in segments:
        raw = seg.get("text", "")
        display = _clean_display_text(raw, case_fn)
        if not display:
            continue
        seg_start = float(seg.get("start", 0.0))
        seg_end = float(seg.get("end", 0.0))
        seg_dur = max(0.1, seg_end - seg_start)
        fontsize = lyric_fontsize(len(display), text_scale, font_scale)
        fade_dur = fade_seconds(lyric_transition, seg_dur)
        fade_ms = int(round(fade_dur * 1000))
        lines.append(AssLine(
            text=display,
            start_s=perceptual_start(seg_start, fade_dur),
            end_s=seg_end,
            fontsize=fontsize,
            fade_in_ms=fade_ms,
            fade_out_ms=fade_ms,
        ))
    return lines


def _ass_time(seconds: float) -> str:
    """Format seconds as ASS H:MM:SS.cs (centiseconds)."""
    if seconds < 0:
        seconds = 0.0
    total_cs = int(round(seconds * 100))
    cs = total_cs % 100
    total_s = total_cs // 100
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_escape(text: str) -> str:
    """Escape a display string for an ASS Dialogue Text field.

    Newlines become hard line breaks (\\N). Braces would open an
    override block, so they are neutralised. We keep it minimal —
    the caller already sanitised case / control chars."""
    return (
        text.replace("\\", "\\\\")
        .replace("{", "(")
        .replace("}", ")")
        .replace("\r\n", "\\N")
        .replace("\n", "\\N")
        .replace("\r", "\\N")
    )


def build_ass(
    *,
    width: int,
    height: int,
    font_name: str,
    base_fontsize: int,
    outline: float,
    shadow: float,
    lines: list[AssLine],
    margin_v: int = 0,
    alignment: int = 5,
    bold: bool = True,
) -> str:
    """Build a complete ASS document for the lyric overlay.

    width/height  : PlayRes, must match the render frame size so libass
                    positions and scales correctly.
    font_name     : libass font family name (must be discoverable via the
                    ffmpeg `fontsdir` passed alongside the subtitles
                    filter).
    base_fontsize : style default; per-line AssLine.fontsize overrides it
                    inline via \\fs (legacy sizes lines by length).
    outline/shadow: border and drop-shadow widths in pixels (the legacy
                    stroke_width and shadow_offset).
    alignment     : numpad alignment; 5 = middle-center (legacy default).
    margin_v      : vertical margin in px (0 for centered \\an5).
    """
    # White fill, black outline, black shadow. BorderStyle 1 = outline +
    # drop shadow (not opaque box).
    style = (
        "Style: Lyric,{font},{fs},"
        "&H00FFFFFF,&H000000FF,&H00000000,&H80000000,"  # primary, secondary, outline, back(shadow, 50% alpha)
        "{bold},0,0,0,"      # bold (-1 true / 0 false), italic, underline, strikeout
        "100,100,0,0,"       # scaleX, scaleY, spacing, angle
        "1,{bord},{shad},"   # BorderStyle=1, Outline, Shadow
        "{align},20,20,{mv},1"  # alignment, marginL, marginR, marginV, encoding
    ).format(
        font=font_name,
        fs=base_fontsize,
        bold=-1 if bold else 0,
        bord=_fmt_num(outline),
        shad=_fmt_num(shadow),
        align=alignment,
        mv=margin_v,
    )

    header = "\n".join(
        [
            "[Script Info]",
            "ScriptType: v4.00+",
            f"PlayResX: {int(width)}",
            f"PlayResY: {int(height)}",
            "WrapStyle: 0",
            "ScaledBorderAndShadow: yes",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
            "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding",
            style,
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
            "MarginV, Effect, Text",
        ]
    )

    events = []
    for ln in lines:
        if not ln.text or not ln.text.strip():
            continue
        if ln.end_s <= ln.start_s:
            continue
        overrides = f"\\fs{int(ln.fontsize)}"
        if ln.fade_in_ms > 0 or ln.fade_out_ms > 0:
            overrides += f"\\fad({int(ln.fade_in_ms)},{int(ln.fade_out_ms)})"
        text = "{" + overrides + "}" + _ass_escape(ln.text)
        events.append(
            "Dialogue: 0,{start},{end},Lyric,,0,0,0,,{text}".format(
                start=_ass_time(ln.start_s),
                end=_ass_time(ln.end_s),
                text=text,
            )
        )

    return header + "\n" + "\n".join(events) + "\n"


def _fmt_num(value: float) -> str:
    """ASS accepts ints or floats; emit a clean compact number."""
    f = float(value)
    if f == int(f):
        return str(int(f))
    return f"{f:.2f}".rstrip("0").rstrip(".")
