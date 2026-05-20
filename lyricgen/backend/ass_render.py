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
        "-1,0,0,0,"          # bold=-1 (true), italic, underline, strikeout
        "100,100,0,0,"       # scaleX, scaleY, spacing, angle
        "1,{bord},{shad},"   # BorderStyle=1, Outline, Shadow
        "{align},20,20,{mv},1"  # alignment, marginL, marginR, marginV, encoding
    ).format(
        font=font_name,
        fs=base_fontsize,
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
