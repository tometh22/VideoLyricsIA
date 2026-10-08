"""Lyric "looks": complete, one-click typographic treatments.

A look bundles everything that makes a lyric video feel designed rather than
subtitled — font, colours, how each line is composed (one row, a small lead
line over a big key word, or words landing one by one in a staggered
layout), how it moves, what sits behind it (the job's background or flat
colour cards) and the colour grade / overlay on top of the background.

Everything typographic is emitted as one ASS document so the render stays a
single libass pass; the grade and the optional overlay effect go through
fx_compositor exactly like the per-option path does.

Timing contract: every line keeps its approved window. Entrances may start a
few ms early (perceptual onset) and exits happen INSIDE the window — no look
moves a lyric's end timestamp.
"""
from __future__ import annotations

import dataclasses
import os
import re
import unicodedata
from dataclasses import dataclass, field

import ass_render as _ass

_FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")


@dataclass(frozen=True)
class Look:
    id: str
    font_file: str
    # Composition: "line" (one block), "keyword" (small lead + big key word),
    # "build" (words land one by one in a staggered layout).
    layout: str
    # Motion: "zoom_through" | "cine" | "word_pop" | "fade".
    motion: str
    primary: str                    # #RRGGBB text fill
    accent: str = ""                # key-word fill ("" = same as primary)
    outline_color: str = "#000000"
    outline: float = 2.0            # px at 1080p
    shadow: float = 2.0             # px at 1080p
    shadow_color: str = "#000000"
    shadow_alpha: int = 0x80        # 0 opaque … 255 invisible
    glow: float = 0.0               # \blur on the outline (soft halo)
    font_scale: float = 1.0
    key_scale: float = 1.45         # key word size vs base
    lead_scale: float = 0.55        # lead line size vs base (keyword layout)
    tilt: float = 0.0               # max block rotation in degrees (build)
    stagger: float = 0.0            # row x-offset as a fraction of width (build)
    row_width: float = 0.70         # max row width as a fraction of width (build)
    # Background: "source" (job background) or "flat" (colour cards that cut
    # on every line — no generated background needed at all).
    background: str = "source"
    flat_colors: tuple[str, ...] = ()
    flat_accents: tuple[str, ...] = ()  # key-word colour per card (contrast)
    letterbox: bool = False         # 2.39:1 bars drawn in the ASS layer
    grade: str = ""                 # ffmpeg filter chain applied to the bg
    effect: str = ""                # fx_compositor overlay id ("" = none)
    tags: tuple[str, ...] = field(default_factory=tuple)


LOOKS: dict[str, Look] = {
    # EARTHGANG "OSMOSIS": the line arrives from far away and flies past the
    # camera at its end.
    "cosmico": Look(
        id="cosmico", font_file="Audiowide-Regular.ttf",
        layout="line", motion="zoom_through",
        primary="#FFFFFF", outline=1.5, shadow=3, font_scale=1.25,
        grade="eq=contrast=1.08:saturation=1.10",
    ),
    # Taylor Swift "Babylon" / "Patient Zero": elegant serif caps, a small
    # lead line over a big key word, entering out of focus with the letters
    # spread wide and closing in. Letterboxed, warm, vignetted.
    "cine": Look(
        id="cine", font_file="Marcellus-Regular.ttf",
        layout="keyword", motion="cine",
        primary="#F6EEE2", outline=2, outline_color="#000000", glow=6,
        shadow=0, font_scale=1.0, key_scale=1.55, lead_scale=0.52,
        letterbox=True,
        grade=("eq=contrast=1.06:saturation=0.82:gamma_r=1.03:gamma_b=0.97,"
               "vignette=angle=PI/4.5"),
    ),
    # Rolling Stones "Paint It, Black" / "Satisfaction": brush caps, words
    # slam in one by one as they are sung, the key word in red, the block
    # slightly tilted; gritty desaturated grade with film grain.
    "pincel": Look(
        id="pincel", font_file="Knewave-Regular.ttf",
        layout="build", motion="word_pop",
        primary="#F4F0E8", accent="#E3262F", outline=3, shadow=4,
        shadow_alpha=0x60, font_scale=1.2, key_scale=1.45, tilt=5.0,
        row_width=0.62,
        grade="eq=contrast=1.16:saturation=0.35:gamma=1.08,vignette=angle=PI/4.5",
        effect="film",
    ),
    # Snakehips & EARTHGANG "Been a Minute": 70s groovy type, cream with a
    # dark outline and a hard retro drop shadow, words stacking in staggered
    # rows over flat colour cards that cut on every line. Needs NO generated
    # background.
    "pop70": Look(
        id="pop70", font_file="Shrikhand-Regular.ttf",
        layout="build", motion="word_pop",
        primary="#FFF3DC", accent="#FFD23F", outline=5, outline_color="#3A1D2E",
        shadow=7, shadow_color="#3A1D2E", shadow_alpha=0,
        font_scale=1.5, key_scale=1.5, stagger=0.07, row_width=0.55,
        background="flat",
        flat_colors=("#EE5FA0", "#6E8EDB", "#8FCB6B", "#F4ECDD"),
        flat_accents=("#FFD23F", "#FFD23F", "#EE5FA0", "#6E8EDB"),
    ),
    # JENNIE "Less than a Lover": faded teal film stock, grain, small
    # condensed yellow caps that simply cut in and out.
    "pelicula": Look(
        id="pelicula", font_file="Oswald-Bold.ttf",
        layout="line", motion="fade",
        primary="#F2C230", outline=0, shadow=2, shadow_alpha=0x90,
        font_scale=0.85,
        grade=("curves=r='0/0.07 0.5/0.47 1/0.90':g='0/0.10 0.5/0.52 1/0.94':"
               "b='0/0.13 0.5/0.50 1/0.86',eq=saturation=0.72,vignette=angle=PI/5"),
        effect="film",
    ),
}


def get_look(look_id: str | None) -> Look | None:
    return LOOKS.get((look_id or "").strip().lower())


def font_path(look: Look) -> str:
    return os.path.join(_FONTS_DIR, look.font_file)


# --- Key word ---------------------------------------------------------------

_STOPWORDS = frozenset("""
a al algo ante como con contra cual cuando de del desde donde e el ella ellas
ellos en entre era es esa ese eso esta este esto fue ha hay la las le les lo
los me mi mis muy nada ni no nos o os para pero por porque que qué se ser si
sin sobre son su sus tan te ti tu tú tus un una uno unos y ya yo él más mas
the and or but a an of to in on at for with from by is are was were be been
i you he she it we they me my your our their his her its this that these
those so as if not no do does did just like oh ooh yeah hey la na da
""".split())


def _norm(tok: str) -> str:
    t = unicodedata.normalize("NFC", tok).lower()
    return "".join(ch for ch in t if ch.isalnum())


def _is_content(tok: str) -> bool:
    n = _norm(tok)
    return len(n) >= 4 and n not in _STOPWORDS


def pick_keyword(tokens: list[str]) -> int | None:
    """Index of the word to emphasise, or None.

    Lyrics put the weight at the end of the line (rhyme, held note), so we
    take the LAST content word. Lines with no content word get no emphasis
    instead of a random big "the"."""
    for i in range(len(tokens) - 1, -1, -1):
        if _is_content(tokens[i]):
            return i
    return None


# --- ASS plumbing -------------------------------------------------------------

def _c(hex_color: str, fallback: str = "&H00FFFFFF") -> str:
    return _ass.hex_to_ass(hex_color, fallback=fallback)


def _a(alpha: int) -> str:
    return f"&H{max(0, min(255, int(alpha))):02X}&"


def _header(width: int, height: int, family: str, fontsize: int, look: Look,
            sc: float) -> str:
    style = (
        f"Style: Lyric,{family},{fontsize},{_c(look.primary)},&H000000FF,"
        f"{_c(look.outline_color, '&H00000000')},"
        f"{_c(look.shadow_color, '&H00000000')[:2]}{look.shadow_alpha:02X}"
        f"{_c(look.shadow_color, '&H00000000')[4:]},"
        "0,0,0,0,100,100,0,0,1,"
        f"{_ass._fmt_num(look.outline * sc)},{_ass._fmt_num(look.shadow * sc)},"
        "5,20,20,0,1"
    )
    return "\n".join([
        "[Script Info]", "ScriptType: v4.00+",
        f"PlayResX: {int(width)}", f"PlayResY: {int(height)}",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        style, "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
        "MarginV, Effect, Text",
    ])


def _dialogue(layer: int, start: float, end: float, text: str) -> str:
    return (f"Dialogue: {layer},{_ass._ass_time(start)},{_ass._ass_time(end)},"
            f"Lyric,,0,0,0,,{text}")


def _rect(width: int, height: int, x0: int, y0: int, x1: int, y1: int,
          color: str) -> str:
    return (f"{{\\an7\\pos(0,0)\\bord0\\shad0\\1c{_c(color)}\\p1}}"
            f"m {x0} {y0} l {x1} {y0} {x1} {y1} {x0} {y1}{{\\p0}}")


# --- Line composition -------------------------------------------------------

def _line_motion(look: Look, dur_ms: int) -> str:
    """Line-level entrance/exit tags (relative to the event start)."""
    if look.motion == "cine":
        enter = min(750, max(250, int(dur_ms * 0.30)))
        exit_ = min(420, max(160, int(dur_ms * 0.18)))
        return ("\\blur10\\fsp22\\alpha&HFF&"
                f"\\t(0,{enter},0.6,\\blur0\\fsp3\\alpha&H00&)"
                f"\\t({dur_ms - exit_},{dur_ms},\\blur8\\alpha&HFF&)")
    if look.motion == "fade":
        f = min(160, max(60, int(dur_ms * 0.08)))
        return f"\\fad({f},{f})"
    return ""


def _keyword_text(look: Look, tokens: list[str], base_fs: int) -> str:
    """Small lead row over a big key word (or the whole short line big)."""
    key_fs = int(round(base_fs * look.key_scale))
    lead_fs = max(18, int(round(base_fs * look.lead_scale)))
    accent = f"\\1c{_c(look.accent)}" if look.accent else ""
    if len(tokens) <= 2:
        return f"{{\\fs{key_fs}{accent}}}" + _ass._ass_escape(" ".join(tokens))
    k = pick_keyword(tokens)
    if k is None or k != len(tokens) - 1:
        # No trailing key word → one calm row at the base size.
        return f"{{\\fs{base_fs}}}" + _ass._ass_escape(" ".join(tokens))
    lead = " ".join(tokens[:k])
    return (f"{{\\fs{lead_fs}}}" + _ass._ass_escape(lead) + "\\N"
            f"{{\\fs{key_fs}{accent}}}" + _ass._ass_escape(tokens[k]))


class _Measure:
    """Word widths from the real font file (PIL), in libass pixels.

    libass sizes a font so that ascender+descender spans \\fs pixels, while
    PIL sizes it by the em square. Without converting, every width comes out
    ~1.2-1.6x too large (depending on the face) and the words of a built
    line drift apart."""

    def __init__(self, path: str):
        from PIL import ImageFont
        self.path = path
        self._fonts: dict[int, object] = {}
        asc, desc = ImageFont.truetype(path, 200).getmetrics()
        self._em_per_fs = 200.0 / max(1, asc + desc)

    def width(self, text: str, size: int) -> float:
        from PIL import ImageFont
        em = max(1, int(round(size * self._em_per_fs)))
        f = self._fonts.get(em)
        if f is None:
            f = self._fonts[em] = ImageFont.truetype(self.path, em)
        return float(f.getlength(text))


def _build_events(look: Look, seg_idx: int, tokens: list[str],
                  timings: list[dict], line_start: float, line_end: float,
                  base_fs: int, width: int, height: int,
                  measure: _Measure) -> list[str]:
    """Words land one by one (at their sung time) in staggered rows."""
    k = pick_keyword(tokens)
    sizes = [int(round(base_fs * (look.key_scale if i == k else 1.0)))
             for i in range(len(tokens))]
    # Word gap: the font's space plus room for the outline and drop shadow
    # on both neighbours (thick retro outlines otherwise glue words together).
    sc = height / 1080.0
    space = (measure.width(" ", base_fs) + 0.08 * base_fs
             + 2 * look.outline * sc + 0.5 * look.shadow * sc)
    max_row = width * look.row_width

    # Greedy rows; a key word always gets its own row when the line is long
    # enough to afford it — that's what makes the composition read as type
    # design instead of a wrapped subtitle.
    rows: list[list[int]] = []
    cur: list[int] = []
    cur_w = 0.0
    for i, tok in enumerate(tokens):
        w = measure.width(tok, sizes[i])
        solo = (i == k and len(tokens) > 2)
        if cur and (solo or cur_w + space + w > max_row
                    or (k is not None and cur[-1] == k and len(tokens) > 2)):
            rows.append(cur)
            cur, cur_w = [], 0.0
        cur.append(i)
        cur_w += (space if len(cur) > 1 else 0) + w
    if cur:
        rows.append(cur)

    row_h = [max(sizes[i] for i in r) * 1.02 for r in rows]
    total_h = sum(row_h)
    y = (height - total_h) / 2.0
    cx, cy = width / 2.0, height / 2.0
    # Deterministic tilt per line (alternating sides), so a re-render of the
    # same song looks identical.
    tilt = 0.0
    if look.tilt:
        tilt = look.tilt * (1 if seg_idx % 2 == 0 else -0.7)

    events: list[str] = []
    for r_i, r in enumerate(rows):
        widths = [measure.width(tokens[i], sizes[i]) for i in r]
        row_w = sum(widths) + space * (len(r) - 1)
        shift = 0.0
        if look.stagger and len(rows) > 1:
            shift = width * look.stagger * (-1 if r_i % 2 == 0 else 1)
        x = (width - row_w) / 2.0 + shift
        baseline = y + row_h[r_i]
        for j, i in enumerate(r):
            w_start = max(line_start, float(timings[i]["start"]) - 0.04)
            if w_start >= line_end:
                w_start = line_start
            dur_ms = max(1, int(round((line_end - w_start) * 1000)))
            exit_ms = min(180, max(80, int(dur_ms * 0.15)))
            px = x + widths[j] / 2.0
            ov = (f"\\an2\\pos({int(round(px))},{int(round(baseline))})"
                  f"\\fs{sizes[i]}")
            accent = look.accent
            if look.flat_accents:
                accent = look.flat_accents[seg_idx % len(look.flat_accents)]
            if i == k and accent:
                ov += f"\\1c{_c(accent)}"
            if tilt:
                ov += f"\\org({int(cx)},{int(cy)})\\frz{_ass._fmt_num(round(tilt, 2))}"
            ov += ("\\fscx135\\fscy135\\alpha&HFF&"
                   "\\t(0,110,0.7,\\fscx100\\fscy100\\alpha&H00&)"
                   f"\\t({dur_ms - exit_ms},{dur_ms},\\alpha&HFF&)")
            events.append(_dialogue(2, w_start, line_end,
                                    "{" + ov + "}" + _ass._ass_escape(tokens[i])))
            x += widths[j] + space
        y += row_h[r_i]
    return events


# --- Public entry point -------------------------------------------------------

def build_look_ass(
    segments: list[dict],
    look: Look,
    *,
    width: int,
    height: int,
    duration: float,
    case_fn=str.upper,
    font_scale: float = 1.0,
    primary_override: str = "",
    title_lines: list | None = None,
) -> str:
    """Complete ASS document for `look` over `segments`.

    primary_override: operator-picked lyric colour (#RRGGBB) that replaces
    the look's text colour; "" keeps the look's own. White (#FFFFFF) also
    keeps it: it is the wizard's default colour and is persisted on every
    job, so it can't be told apart from "not chosen".
    title_lines: the artist/song title card (ass_render.AssLine list) — drawn
    above everything else so every job keeps a readable title card."""
    if (primary_override and re.match(r"^#[0-9a-fA-F]{6}$", primary_override)
            and primary_override.upper() != "#FFFFFF"):
        look = dataclasses.replace(look, primary=primary_override)
    sc = height / 1080.0
    path = font_path(look)
    family, _bold = _ass.font_family(path)
    measure = _Measure(path)
    fs_mult = look.font_scale * max(0.6, min(1.5, float(font_scale or 1.0)))
    header = _header(width, height, family,
                     _ass.lyric_fontsize(40, sc, fs_mult), look, sc)

    lines = []
    for seg in segments:
        display = _ass._clean_display_text(seg.get("text", ""), case_fn)
        if not display:
            continue
        start, end = float(seg.get("start", 0.0)), float(seg.get("end", 0.0))
        if end <= start:
            continue
        lines.append((seg, display, start, end))

    events: list[str] = []

    if look.background == "flat" and look.flat_colors:
        # Colour cards cut on every line; the card holds through the gap
        # until the next line so the screen never flashes the source bg.
        for n, (_seg, _d, start, _end) in enumerate(lines):
            card_start = 0.0 if n == 0 else start
            card_end = lines[n + 1][2] if n + 1 < len(lines) else duration
            color = look.flat_colors[n % len(look.flat_colors)]
            events.append(_dialogue(0, card_start, card_end,
                                    _rect(width, height, 0, 0, width, height, color)))

    if look.letterbox and width > height:
        # Cinema bars only make sense on a horizontal frame; on a vertical
        # short they would eat two thirds of the picture.
        bar = int(round((height - width / 2.39) / 2))
        if bar > 0:
            events.append(_dialogue(5, 0, duration,
                                    _rect(width, height, 0, 0, width, bar, "#000000")))
            events.append(_dialogue(5, 0, duration,
                                    _rect(width, height, 0, height - bar, width, height, "#000000")))

    for n, (seg, display, start, end) in enumerate(lines):
        tokens = display.split()
        base_fs = _ass.lyric_fontsize(len(display), sc, fs_mult)
        if look.layout == "build":
            timings = _ass._word_timings(display, start, end, seg.get("words"))
            events.extend(_build_events(look, n, tokens, timings, start, end,
                                        base_fs, width, height, measure))
            continue
        dur_ms = max(1, int(round((end - start) * 1000)))
        glow = (f"\\3a&HB0&\\blur{_ass._fmt_num(look.glow)}" if look.glow else "")
        if look.layout == "keyword":
            body = _keyword_text(look, tokens, base_fs)
            text = "{" + "\\an5" + glow + _line_motion(look, dur_ms) + "}" + body
            events.append(_dialogue(2, start, end, text))
            continue
        # "line" layout.
        if look.motion == "zoom_through":
            line = _ass.AssLine(text=display, start_s=start, end_s=end,
                                fontsize=base_fs, transition="zoom_through")
            doc = _ass.build_ass(width=width, height=height, font_name=family,
                                 base_fontsize=base_fs, outline=look.outline * sc,
                                 shadow=look.shadow * sc, lines=[line], bold=False,
                                 primary_color=look.primary)
            body = [ln for ln in doc.splitlines() if ln.startswith("Dialogue:")][0]
            events.append(body.replace("Dialogue: 0,", "Dialogue: 2,", 1))
            continue
        text = ("{" + f"\\fs{base_fs}" + glow + _line_motion(look, dur_ms) + "}"
                + _ass._ass_escape(display))
        events.append(_dialogue(2, start, end, text))

    if title_lines:
        doc = _ass.build_ass(width=width, height=height, font_name=family,
                             base_fontsize=_ass.lyric_fontsize(40, sc, fs_mult),
                             outline=look.outline * sc, shadow=look.shadow * sc,
                             lines=list(title_lines), bold=False,
                             primary_color=look.primary)
        for ln in doc.splitlines():
            if ln.startswith("Dialogue: "):
                # Above the cards (0), the lyrics (2) AND the letterbox bars
                # (5): a low title card must never end up behind a bar.
                events.append("Dialogue: 6," + ln.split(",", 1)[1])

    return header + "\n" + "\n".join(events) + "\n"
