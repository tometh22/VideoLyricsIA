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
    # "build" (words land one by one in a staggered layout), "chat" (message
    # bubbles that stack up), "block" (rows stretched to one width), "arc"
    # (the line curves around a ring), "floor" (text lying in 3D perspective).
    layout: str
    # Motion: "zoom_through" | "cine" | "word_pop" | "fade" | "neon" |
    # "write_on" | "boil".
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
    bold: bool = False              # style Bold (static bold instances)
    extra_fonts: tuple[str, ...] = ()  # more font files the look switches to
    force_case: str = ""            # "original" keeps the lyric's own case
    line_colors: tuple[str, ...] = ()  # per-line colour cycle (neon tubes)
    word_motion: str = "pop"        # build layout: "pop" | "blur"
    word_tilt: float = 0.0          # build layout: per-word hand-placed tilt
    circle_key: bool = False        # build layout: hand-drawn ring on key word
    doodles: bool = False           # build layout: little hearts/stars
    gradient: tuple[str, ...] = ()  # background="gradient": top → bottom
    card_palettes: tuple[tuple[str, ...], ...] = ()  # (ink, alt, accent) per card
    beat_sync: bool = False         # needs the song's beat times
    # How the generated background should be steered for this look (palette,
    # mood, where to leave room for the text). Consumed by the background
    # prompt; "" = no steering.
    bg_hint: str = ""


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
    # Ed Sheeran "Shape of You" / Imagine Dragons "Sharks": kinetic type —
    # words land one by one from different directions and build a
    # composition (script connectors, heavy caps stretched to a block, the
    # key word huge with a burst, bubble or swoosh, kicking on the beat),
    # over flat illustrated cards that alternate navy and sky blue.
    "cinetico": Look(
        id="cinetico", font_file="BigShouldersDisplay-Black.ttf",
        extra_fonts=("Caveat-Bold.ttf",),
        layout="kinetic", motion="word_pop", beat_sync=True,
        primary="#FFFFFF", outline=0, shadow=4, shadow_color="#0B0D22",
        shadow_alpha=0x40, font_scale=1.0,
        background="flat", flat_colors=("#1F2244", "#86C8EE"),
        card_palettes=(("#FFFFFF", "#6EC6FF", "#FF7A45"),
                       ("#1F2244", "#FFFFFF", "#E63946")),
        effect="film",
    ),
    # Ed Sheeran "Body": each line is a neon tube that flickers on and hums,
    # cycling tube colours line to line over a dimmed background.
    "neon": Look(
        id="neon", font_file="TiltNeon-Regular.ttf",
        extra_fonts=("Neonderthaw-Regular.ttf",),
        layout="line", motion="neon", beat_sync=True, force_case="original",
        primary="#FFF4FA", outline=0, shadow=0, font_scale=1.9,
        line_colors=("#FF3EA5", "#2EE6FF", "#B07BFF", "#FFB13B"),
        grade="eq=brightness=-0.20:saturation=0.70:contrast=1.08,vignette=angle=PI/4",
        bg_hint="low-key exposure, deep shadows and dark negative space in the "
                "centre of the frame so glowing tubes read clearly",
    ),
    # Ed Sheeran "Shape of You" phone: every line arrives as a message bubble
    # and the conversation scrolls up. Built for vertical shorts.
    "chat": Look(
        id="chat", font_file="Roboto-Bold.ttf",
        layout="chat", motion="fade", force_case="original",
        primary="#111111", accent="#FFFFFF", outline=0, shadow=0,
        font_scale=0.78,
        flat_colors=("#E9E9EB", "#1F8BFF"),     # incoming / outgoing bubble
        grade="boxblur=16:2,eq=brightness=-0.16:saturation=0.85",
        bg_hint="soft, out-of-focus everyday scene, shallow depth of field",
    ),
    # ROSALÍA "LLYLM" + Shakira "Dai Dai": handwriting landing word by word,
    # each word placed by hand, the key word circled in red marker and a
    # couple of doodles popping around it.
    "cuaderno": Look(
        id="cuaderno", font_file="Caveat-Bold.ttf", bold=True,
        layout="build", motion="word_pop", force_case="original",
        primary="#FFFFFF", accent="#FFFFFF", outline=1.5, shadow=3,
        shadow_alpha=0x50, font_scale=1.75, key_scale=1.35, row_width=0.62,
        word_tilt=3.0, circle_key=True, doodles=True, word_motion="write",
        grade="eq=contrast=1.04:saturation=0.90",
        bg_hint="intimate, candid, warm indoor light, analog photo feel",
    ),
    # Maroon 5 "Maps" / Ed Sheeran: kinetic-type block — every row stretched
    # to the same width, heavy and light weights alternating, the key row in
    # coral. Rows slam in as they are sung.
    "bloque": Look(
        id="bloque", font_file="BigShouldersDisplay-Black.ttf",
        extra_fonts=("BigShouldersDisplay-Light.ttf",),
        layout="block", motion="word_pop",
        primary="#FFFFFF", accent="#FF6B5B", outline=0, shadow=4,
        shadow_alpha=0x70, font_scale=1.0, row_width=0.46,
        grade="eq=brightness=-0.08:contrast=1.06:saturation=0.9",
        bg_hint="graphic, minimal composition with a large calm area for type",
    ),
    # Imagine Dragons "Sharks": the line curves around a ring that slowly
    # turns, the key word big in the middle.
    "arco": Look(
        id="arco", font_file="Montserrat-ExtraBold.ttf",
        layout="arc", motion="fade",
        primary="#FFFFFF", accent="#FFD23F", outline=2, shadow=3,
        shadow_alpha=0x70, font_scale=0.85, key_scale=2.1,
        grade="eq=contrast=1.08:saturation=0.85,vignette=angle=PI/4",
        bg_hint="centered, symmetrical composition with an empty centre",
    ),
    # Maroon 5 "Maps": words lying on the ground in 3D, the camera flying
    # forward over them.
    "perspectiva": Look(
        id="perspectiva", font_file="Montserrat-ExtraBold.ttf",
        layout="floor", motion="fade",
        primary="#FFFFFF", accent="#7CF5D6", outline=4, outline_color="#0B4F47",
        shadow=0, font_scale=1.5,
        grade="eq=contrast=1.06:saturation=0.9",
        bg_hint="wide aerial or ground-level view with a visible floor or road "
                "receding to the horizon",
    ),
    # Shakira "Dai Dai": the background becomes a two-ink poster (blue on
    # cream) and the marker lettering "boils" like hand animation.
    "duotono": Look(
        id="duotono", font_file="PermanentMarker-Regular.ttf",
        layout="line", motion="boil",
        line_colors=("#FFF7E6", "#FFD23F", "#FF8FB1", "#9EF0C8"),
        primary="#FFF7E6", outline=4, outline_color="#1B2C7A",
        shadow=5, shadow_color="#1B2C7A", shadow_alpha=0,
        font_scale=1.8,
        grade=("hue=s=0,eq=contrast=1.35:brightness=0.03,"
               "curves=r='0/0.09 1/0.97':g='0/0.19 1/0.95':b='0/0.60 1/0.89'"),
        bg_hint="high-contrast scene with clear silhouettes and strong shapes",
    ),
    # Ellie Goulding: a romantic script writes itself on, letterboxed, soft
    # and warm.
    "romantico": Look(
        id="romantico", font_file="Sacramento-Regular.ttf",
        layout="line", motion="write_on", force_case="original",
        primary="#FFFFFF", outline=2, outline_color="#FFD9C2", glow=5,
        shadow=2, shadow_alpha=0x90, font_scale=2.4, letterbox=True,
        grade=("eq=contrast=0.94:saturation=0.85:brightness=0.02,"
               "curves=all='0/0.05 1/0.98',vignette=angle=PI/5"),
        bg_hint="soft romantic light, golden hour, gentle bokeh",
    ),
    # Sabrina Carpenter "Manchild": sunset gradient with a mountain silhouette
    # and clean condensed caps. No generated background at all.
    "degrade": Look(
        id="degrade", font_file="Oswald-Bold.ttf",
        layout="line", motion="fade",
        primary="#FFFFFF", outline=0, shadow=2, shadow_alpha=0x90,
        font_scale=1.35, background="gradient",
        gradient=("#9D4EDD", "#C850C0", "#FF4F8B"),
    ),
    # Backstreet Boys "I Want It That Way": Y2K icy chrome — wide techno
    # caps blurring in word by word over an icy, light-leaked grade.
    "y2k": Look(
        id="y2k", font_file="Michroma-Regular.ttf",
        layout="build", motion="word_pop", word_motion="echo",
        primary="#FFFFFF", accent="#BFF6FF", outline=2.5, outline_color="#5ED8FF",
        glow=4, shadow=3, shadow_color="#1E6BFF", shadow_alpha=0x70,
        font_scale=1.05, key_scale=1.3, stagger=0.08, row_width=0.66,
        grade=("colorbalance=rs=-0.12:gs=0.02:bs=0.18:rm=-0.08:bm=0.12,"
               "eq=brightness=0.06:saturation=0.75:contrast=0.95"),
        effect="light",
        bg_hint="bright, airy, icy blue and white tones, glossy reflections",
    ),
}


def get_look(look_id: str | None) -> Look | None:
    return LOOKS.get((look_id or "").strip().lower())


WORD_TIMED_LAYOUTS = ("build", "kinetic", "block", "arc")


def needs_word_timings(look_id: str | None) -> bool:
    """True when the look places words at their sung time, so the pipeline
    must run the (cached) forced alignment like it does for karaoke."""
    look = get_look(look_id)
    return look is not None and look.layout in WORD_TIMED_LAYOUTS


def owns_background(look_id: str | None) -> bool:
    """True when the look paints the whole frame itself (flat cards or a
    gradient), so a generated background would never be seen."""
    look = get_look(look_id)
    return look is not None and look.background in ("flat", "gradient")


def font_path(look: Look) -> str:
    return os.path.join(_FONTS_DIR, look.font_file)


def font_paths(look: Look) -> list[str]:
    """Every font file the look's ASS can reference (for the fontsdir)."""
    return [font_path(look)] + [os.path.join(_FONTS_DIR, f) for f in look.extra_fonts]


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
        f"{-1 if look.bold else 0},0,0,0,100,100,0,0,1,"
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


_T_RE = None


def _clamp_times(text: str, dur_ms: int) -> str:
    """Keep every animation inside its event: \\t(a,b,…) and the timed
    \\move(…,t1,t2) get 0 <= a < b <= dur. Short lines and late words used to
    produce reversed or negative windows, so a fade-out never finished."""
    import re
    global _T_RE
    if _T_RE is None:
        _T_RE = (re.compile(r"\\t\((-?\d+),(-?\d+),"),
                 re.compile(r"(\\move\(-?\d+,-?\d+,-?\d+,-?\d+),(-?\d+),(-?\d+)\)"))
    dur = max(2, int(dur_ms))

    def window(a: int, b: int) -> tuple[int, int]:
        a = max(0, min(a, dur - 1))
        b = max(a + 1, min(b, dur))
        return a, b

    def fix_t(m):
        a, b = window(int(m.group(1)), int(m.group(2)))
        return f"\\t({a},{b},"

    def fix_move(m):
        a, b = window(int(m.group(2)), int(m.group(3)))
        return f"{m.group(1)},{a},{b})"

    return _T_RE[1].sub(fix_move, _T_RE[0].sub(fix_t, text))


def _dialogue(layer: int, start: float, end: float, text: str) -> str:
    """One ASS event. Events that would start at/after their end (a
    decoration scheduled past a short line) are dropped ("")."""
    if end - start < 0.02:
        return ""
    text = _clamp_times(text, int(round((end - start) * 1000)))
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


def _em_per_fs(path: str) -> float:
    """Em size per libass \\fs unit for a font file.

    libass sizes a face so that usWinAscent + usWinDescent (OS/2) span \\fs
    pixels, falling back to the hhea ascender/descender when the face has no
    OS/2 table. PIL's getmetrics() reports hhea, which differs a lot on some
    display faces (Big Shoulders: 1.20 vs 1.67 em) — measuring with it put
    letters ~28% too far apart."""
    import struct
    try:
        with open(path, "rb") as fh:
            data = fh.read()
        num = struct.unpack(">H", data[4:6])[0]
        tables = {}
        for i in range(num):
            tag, _cs, off, ln = struct.unpack(">4sIII", data[12 + 16 * i:28 + 16 * i])
            tables[tag] = (off, ln)
        upm = struct.unpack(">H", data[tables[b"head"][0] + 18:tables[b"head"][0] + 20])[0]
        if b"OS/2" in tables:
            off = tables[b"OS/2"][0]
            win_asc, win_desc = struct.unpack(">HH", data[off + 74:off + 78])
            span = win_asc + win_desc
        else:
            off = tables[b"hhea"][0]
            asc, desc = struct.unpack(">hh", data[off + 4:off + 8])
            span = asc - desc
        if span > 0 and upm > 0:
            return upm / float(span)
    except Exception:
        pass
    from PIL import ImageFont
    asc, desc = ImageFont.truetype(path, 200).getmetrics()
    return 200.0 / max(1, asc + desc)


class _Measure:
    """Word widths from the real font file (PIL), in libass pixels.

    libass sizes a font so that ascender+descender spans \\fs pixels, while
    PIL sizes it by the em square. Without converting, every width comes out
    ~1.2-1.6x too large (depending on the face) and the words of a built
    line drift apart."""

    def __init__(self, path: str):
        self.path = path
        self._fonts: dict[int, object] = {}
        self._em_per_fs = _em_per_fs(path)

    def width(self, text: str, size: int) -> float:
        from PIL import ImageFont
        em = max(1, int(round(size * self._em_per_fs)))
        f = self._fonts.get(em)
        if f is None:
            f = self._fonts[em] = ImageFont.truetype(self.path, em)
        return float(f.getlength(text))


def _deco_start(wanted: float, line_start: float, line_end: float,
                min_on: float = 0.6) -> float:
    """When a decoration (marker ring, doodles, bursts) appears: as wanted,
    but early enough to stay on screen `min_on` seconds before the line ends.
    The key word is usually the LAST word, so "after the word" alone often
    landed right at the end of the line."""
    latest = max(line_start, line_end - min_on)
    return max(line_start, min(wanted, latest))


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
    sc = min(width, height) / 1080.0
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
            elif look.word_tilt:
                # Hand-placed: each word sits at its own small angle
                # (deterministic per line/word so re-renders match).
                wt = look.word_tilt * (((i * 37 + seg_idx * 11) % 7) - 3) / 3.0
                ov += f"\\frz{_ass._fmt_num(round(wt, 2))}"
            if look.glow:
                ov += f"\\3a&H40&\\blur{_ass._fmt_num(look.glow * sc)}"
            if look.word_motion == "write":
                # Handwriting: the word is revealed left to right as if
                # written, at roughly the speed it is sung.
                wr = min(420, max(160, int(round(
                    (float(timings[i]["end"]) - float(timings[i]["start"])) * 1000))))
                x0 = px - widths[j] / 2 - sizes[i] * 0.3
                x1 = px + widths[j] / 2 + sizes[i] * 0.3
                ov += (f"\\clip({_n(x0)},0,{_n(x0)},{height})"
                       f"\\t(0,{wr},\\clip({_n(x0)},0,{_n(x1)},{height}))"
                       f"\\t({dur_ms - exit_ms},{dur_ms},\\alpha&HFF&)")
                events.append(_dialogue(2, w_start, line_end,
                                        "{" + ov + "}" + _ass._ass_escape(tokens[i])))
                if i == k and look.circle_key:
                    events.extend(_circle_key_events(look, px, baseline, widths[j],
                                                     sizes[i], _deco_start(w_start + wr / 1000.0,
                                                                           line_start, line_end),
                                                     line_end, sc))
                x += widths[j] + space
                continue
            if look.word_motion == "echo":
                # Y2K ghosting: two translucent echoes slide in from either
                # side and converge on the word, then spread out on exit.
                drift = int(round(70 * sc))
                bx, by = int(round(px)), int(round(baseline))
                base_ov = ov.replace(f"\\pos({bx},{by})", "")
                for sgn, a in ((-1, 0x90), (1, 0xB0)):
                    gx = bx + sgn * drift
                    ghost = (f"\\move({gx},{by},{bx},{by},0,360)" + base_ov
                             + f"\\alpha&HFF&\\t(0,120,\\alpha{_a(a)})"
                             f"\\t(360,520,\\alpha&HFF&)"
                             f"\\blur{_ass._fmt_num(6 * sc)}")
                    events.append(_dialogue(1, w_start, line_end,
                                            "{" + ghost + "}" + _ass._ass_escape(tokens[i])))
                    gx2 = bx - sgn * drift
                    out_ms = max(1, dur_ms - exit_ms - 120)
                    ghost_out = (f"\\move({bx},{by},{gx2},{by},{out_ms},{dur_ms})" + base_ov
                                 + f"\\alpha&HFF&\\t({out_ms},{out_ms + 1},\\alpha{_a(a)})"
                                 f"\\t({out_ms + 1},{dur_ms},\\alpha&HFF&)"
                                 f"\\blur{_ass._fmt_num(6 * sc)}")
                    events.append(_dialogue(1, w_start, line_end,
                                            "{" + ghost_out + "}" + _ass._ass_escape(tokens[i])))
                ov += ("\\alpha&HFF&\\t(140,360,\\alpha&H00&)"
                       f"\\blur{_ass._fmt_num(8 * sc)}\\t(140,360,\\blur{_ass._fmt_num(look.glow * sc or 0.6)})"
                       f"\\t({dur_ms - exit_ms},{dur_ms},\\alpha&HFF&)")
            elif look.word_motion == "blur":
                drift = int(round(26 * sc))
                ov = ov.replace(f"\\pos({int(round(px))},{int(round(baseline))})",
                                f"\\move({int(round(px + drift))},{int(round(baseline))},"
                                f"{int(round(px))},{int(round(baseline))},0,260)")
                ov += ("\\alpha&HFF&\\t(0,240,0.6,\\alpha&H00&)"
                       + (f"\\blur{_ass._fmt_num(12 * sc)}"
                          f"\\t(0,240,\\blur{_ass._fmt_num(look.glow * sc or 0.6)})")
                       + f"\\t({dur_ms - exit_ms},{dur_ms},\\alpha&HFF&)")
            else:
                ov += ("\\fscx135\\fscy135\\alpha&HFF&"
                       "\\t(0,110,0.7,\\fscx100\\fscy100\\alpha&H00&)"
                       f"\\t({dur_ms - exit_ms},{dur_ms},\\alpha&HFF&)")
            events.append(_dialogue(2, w_start, line_end,
                                    "{" + ov + "}" + _ass._ass_escape(tokens[i])))
            if i == k and look.circle_key:
                events.extend(_circle_key_events(look, px, baseline, widths[j],
                                                 sizes[i], _deco_start(w_start + 0.12, line_start,
                                                                       line_end), line_end, sc))
            x += widths[j] + space
        y += row_h[r_i]
    if look.doodles and k is not None and len(tokens) > 1:
        block_w = max(sum(measure.width(tokens[i], sizes[i]) for i in r)
                      + space * (len(r) - 1) for r in rows)
        top = (height - total_h) / 2.0
        k_start = max(line_start, float(timings[k]["start"]))
        events.extend(_doodle_events(seg_idx, (width - block_w) / 2.0,
                                     (width + block_w) / 2.0, top, top + total_h,
                                     base_fs, _deco_start(k_start + 0.25, line_start, line_end),
                                     line_end, sc))
    return events


# --- Vector shapes ------------------------------------------------------------

def _n(v: float) -> str:
    return str(int(round(v)))


def _rounded_rect_path(w: float, h: float, r: float, tail: str = "") -> str:
    """Closed rounded rectangle (0,0)-(w,h); optional speech tail at the
    bottom "left"/"right" corner."""
    r = min(r, w / 2, h / 2)
    k = r * 0.45
    p = (f"m {_n(r)} 0 l {_n(w - r)} 0 b {_n(w - k)} 0 {_n(w)} {_n(k)} {_n(w)} {_n(r)} "
         f"l {_n(w)} {_n(h - r)} ")
    if tail == "right":
        p += (f"b {_n(w)} {_n(h - k)} {_n(w + r * 0.2)} {_n(h)} {_n(w + r * 0.45)} {_n(h + r * 0.25)} "
              f"b {_n(w - r * 0.3)} {_n(h + r * 0.05)} {_n(w - r)} {_n(h)} {_n(w - r)} {_n(h)} ")
    else:
        p += f"b {_n(w)} {_n(h - k)} {_n(w - k)} {_n(h)} {_n(w - r)} {_n(h)} "
    p += f"l {_n(r)} {_n(h)} "
    if tail == "left":
        p += (f"b {_n(r)} {_n(h)} {_n(r * 0.3)} {_n(h + r * 0.05)} {_n(-r * 0.45)} {_n(h + r * 0.25)} "
              f"b {_n(-r * 0.2)} {_n(h)} {_n(0)} {_n(h - k)} {_n(0)} {_n(h - r)} ")
    else:
        p += f"b {_n(k)} {_n(h)} 0 {_n(h - k)} 0 {_n(h - r)} "
    p += f"l 0 {_n(r)} b 0 {_n(k)} {_n(k)} 0 {_n(r)} 0"
    return p


def _ellipse_path(cx: float, cy: float, rx: float, ry: float) -> str:
    k = 0.5523
    return (f"m {_n(cx)} {_n(cy - ry)} "
            f"b {_n(cx + rx * k)} {_n(cy - ry)} {_n(cx + rx)} {_n(cy - ry * k)} {_n(cx + rx)} {_n(cy)} "
            f"b {_n(cx + rx)} {_n(cy + ry * k)} {_n(cx + rx * k)} {_n(cy + ry)} {_n(cx)} {_n(cy + ry)} "
            f"b {_n(cx - rx * k)} {_n(cy + ry)} {_n(cx - rx)} {_n(cy + ry * k)} {_n(cx - rx)} {_n(cy)} "
            f"b {_n(cx - rx)} {_n(cy - ry * k)} {_n(cx - rx * k)} {_n(cy - ry)} {_n(cx)} {_n(cy - ry)}")


def _heart_path(s: float) -> str:
    return (f"m 0 {_n(s * 0.3)} b 0 {_n(-s * 0.1)} {_n(s * 0.5)} {_n(-s * 0.1)} {_n(s * 0.5)} {_n(s * 0.25)} "
            f"b {_n(s * 0.5)} {_n(-s * 0.1)} {_n(s)} {_n(-s * 0.1)} {_n(s)} {_n(s * 0.3)} "
            f"b {_n(s)} {_n(s * 0.6)} {_n(s * 0.6)} {_n(s * 0.8)} {_n(s * 0.5)} {_n(s)} "
            f"b {_n(s * 0.4)} {_n(s * 0.8)} 0 {_n(s * 0.6)} 0 {_n(s * 0.3)}")


def _star_path(s: float) -> str:
    import math
    pts = []
    for i in range(10):
        r = s / 2 if i % 2 == 0 else s / 4.6
        a = -math.pi / 2 + i * math.pi / 5
        pts.append((s / 2 + r * math.cos(a), s / 2 + r * math.sin(a)))
    return "m " + " l ".join(f"{_n(x)} {_n(y)}" for x, y in pts)


def _boil(dur_ms: int, *, step: int = 125, amp: float = 1.2, seed: int = 0) -> str:
    """Stop-motion "boil": the drawing jumps between a few hand-placed poses
    every `step` ms, like hand-drawn animation (instant \\t jumps)."""
    poses = ((-1.0, 100.0), (0.7, 101.4), (-0.4, 99.2), (1.0, 100.8))
    out = []
    i = seed
    for t in range(step, max(step, dur_ms), step):
        a, s = poses[i % len(poses)]
        out.append(f"\\t({t},{t + 1},\\frz{_ass._fmt_num(round(a * amp, 2))}"
                   f"\\fscx{_ass._fmt_num(s)}\\fscy{_ass._fmt_num(s)})")
        i += 1
    return "".join(out)


def _drawing(layer: int, start: float, end: float, overrides: str, path: str) -> str:
    return _dialogue(layer, start, end,
                     "{" + "\\bord0\\shad0" + overrides + "\\p1}" + path + "{\\p0}")


# --- Neon --------------------------------------------------------------------

def _neon_events(look: Look, n: int, display: str, start: float, end: float,
                 base_fs: int, width: int, height: int, sc: float) -> list[str]:
    """A neon tube: a wide coloured haze, a tighter glow and a pale core,
    flickering on together and humming off at the end of the line."""
    col = look.line_colors[n % len(look.line_colors)] if look.line_colors else look.primary
    dur = max(1, int(round((end - start) * 1000)))
    text = _ass._ass_escape(_ass._balanced_breaks(display, base_fs, width))
    pos = f"\\an5\\pos({width // 2},{height // 2})\\q2\\fs{base_fs}"

    def flick(on: int) -> str:
        # on = this layer's alpha when lit; flickers dim to a mix of on/off.
        dim = min(255, on + 0xB0)
        half = min(255, on + 0x70)
        return (f"\\alpha&HFF&\\t(0,1,\\alpha{_a(on)})\\t(70,71,\\alpha{_a(dim)})"
                f"\\t(120,121,\\alpha{_a(on)})\\t(230,231,\\alpha{_a(half)})"
                f"\\t(270,271,\\alpha{_a(on)})"
                f"\\t({max(300, dur - 220)},{dur},\\alpha&HFF&)")

    haze = (pos + f"\\1c{_c(col)}\\3c{_c(col)}\\bord{_ass._fmt_num(14 * sc)}"
            f"\\blur{_ass._fmt_num(26 * sc)}\\shad0" + flick(0x78))
    glow = (pos + f"\\1c{_c(col)}\\3c{_c(col)}\\bord{_ass._fmt_num(5 * sc)}"
            f"\\blur{_ass._fmt_num(9 * sc)}\\shad0" + flick(0x30))
    core = (pos + f"\\1c{_c(look.primary)}\\3c{_c(col)}\\bord{_ass._fmt_num(1.8 * sc)}"
            f"\\blur{_ass._fmt_num(1.2 * sc)}\\shad0" + flick(0))
    return [_dialogue(1, start, end, "{" + haze + "}" + text),
            _dialogue(2, start, end, "{" + glow + "}" + text),
            _dialogue(3, start, end, "{" + core + "}" + text)]


# --- Chat ----------------------------------------------------------------------

def _wrap_rows(tokens: list[str], fs: int, max_w: float, measure) -> list[str]:
    rows: list[str] = []
    cur = ""
    for tok in tokens:
        cand = (cur + " " + tok).strip()
        if cur and measure.width(cand, fs) > max_w:
            rows.append(cur)
            cur = tok
        else:
            cur = cand
    if cur:
        rows.append(cur)
    return rows


def _chat_events(look: Look, lines: list, width: int, height: int, sc: float,
                 fs: int, measure, duration: float) -> list[str]:
    """Message bubbles: each line arrives as a bubble at the bottom (sides
    alternate like a conversation) and the older ones scroll up and dim."""
    vertical = height > width
    max_text_w = width * (0.66 if vertical else 0.42)
    pad_x, pad_y = fs * 0.55, fs * 0.32
    gap = fs * 0.32
    margin = width * (0.07 if vertical else 0.22)
    y_base = height * (0.80 if vertical else 0.84)
    incoming, outgoing = (look.flat_colors + ("#E9E9EB", "#1F8BFF"))[:2]

    bubbles = []
    # Out-of-order or overlapping lines must not break the stack order.
    lines = sorted(lines, key=lambda ln: (ln[2], ln[3]))
    for n, (_seg, display, start, end) in enumerate(lines):
        rows = _wrap_rows(display.split(), fs, max_text_w, measure)
        w = max(measure.width(r, fs) for r in rows) + 2 * pad_x
        h = len(rows) * fs * 1.04 + 2 * pad_y
        right = n % 2 == 1
        x = width - margin - w if right else margin
        bubbles.append(dict(rows=rows, w=w, h=h, x=x, right=right,
                            start=start, end=end))

    events: list[str] = []
    for i, b in enumerate(bubbles):
        bottom = y_base
        prev_top = None
        for j in range(i, len(bubbles)):
            if j > i:
                bottom -= bubbles[j]["h"] + gap
            top = bottom - b["h"]
            if top + b["h"] < height * 0.06:
                break
            t0 = bubbles[j]["start"]
            t1 = (bubbles[j + 1]["start"] if j + 1 < len(bubbles)
                  else min(duration, bubbles[j]["end"] + 1.5))
            if t1 <= t0:
                prev_top = top
                continue
            fill = outgoing if b["right"] else incoming
            ink = look.accent if b["right"] else look.primary
            if j == i:
                motion = (f"\\move({_n(b['x'])},{_n(top + 40 * sc)},{_n(b['x'])},{_n(top)},0,200)"
                          "\\fad(140,0)")
                text_motion = (f"\\move({_n(b['x'] + pad_x)},{_n(top + pad_y + 40 * sc)},"
                               f"{_n(b['x'] + pad_x)},{_n(top + pad_y)},0,200)\\fad(140,0)")
            else:
                if prev_top is None:
                    prev_top = top
                dim = "\\alpha&H00&\\t(0,220,\\alpha&H55&)" if j == i + 1 else "\\alpha&H55&"
                motion = (f"\\move({_n(b['x'])},{_n(prev_top)},{_n(b['x'])},{_n(top)},0,220)" + dim)
                text_motion = (f"\\move({_n(b['x'] + pad_x)},{_n(prev_top + pad_y)},"
                               f"{_n(b['x'] + pad_x)},{_n(top + pad_y)},0,220)" + dim)
            events.append(_drawing(
                1, t0, t1, f"\\an7{motion}\\1c{_c(fill)}",
                _rounded_rect_path(b["w"], b["h"], fs * 0.62,
                                   tail="right" if b["right"] else "left")))
            text = "\\N".join(_ass._ass_escape(r) for r in b["rows"])
            events.append(_dialogue(
                2, t0, t1,
                "{" + f"\\an7{text_motion}\\q2\\fs{fs}\\1c{_c(ink)}\\bord0\\shad0" + "}"
                + text))
            prev_top = top
    return events


# --- Block (kinetic type) -----------------------------------------------------

def _block_events(look: Look, tokens: list[str], timings: list[dict],
                  start: float, end: float, base_fs: int, width: int,
                  height: int, sc: float, heavy, light,
                  light_family: str = "") -> list[str]:
    """Rows of 1-3 words, each stretched to the same block width; heavy and
    light weights alternate, the key word gets its own coral row."""
    k = pick_keyword(tokens)
    rows: list[list[int]] = []
    cur: list[int] = []
    for i, tok in enumerate(tokens):
        if i == k and len(tokens) > 1:
            if cur:
                rows.append(cur)
                cur = []
            rows.append([i])
            continue
        cand = " ".join(tokens[j] for j in cur + [i])
        if cur and len(cand) > 11:
            rows.append(cur)
            cur = [i]
        else:
            cur.append(i)
    if cur:
        rows.append(cur)

    block_w = width * (0.78 if height > width else look.row_width)
    specs = []
    for r_i, r in enumerate(rows):
        text = " ".join(tokens[i] for i in r)
        is_key = (k in r) and len(r) == 1 and len(tokens) > 1
        weight_heavy = is_key or r_i % 2 == 1 or len(rows) == 1
        m = heavy if weight_heavy else light
        nat = max(1.0, m.width(text, 100))
        fs = 100 * block_w / nat
        fs = max(base_fs * 0.55, min(base_fs * 4.5, fs))
        specs.append(dict(text=text, fs=fs, heavy=weight_heavy, key=is_key,
                          first=r[0]))
    total = sum(sp["fs"] * 0.84 for sp in specs)
    limit = height * 0.80
    if total > limit:
        f = limit / total
        for sp in specs:
            sp["fs"] *= f
        total = limit

    events: list[str] = []
    y = (height - total) / 2.0
    dur_total = end - start
    for sp in specs:
        y += sp["fs"] * 0.84
        w_start = max(start, float(timings[sp["first"]]["start"]) - 0.04)
        if w_start >= end:
            w_start = start
        dur = max(1, int(round((end - w_start) * 1000)))
        exit_ms = min(180, max(80, int(dur_total * 1000 * 0.08)))
        ov = (f"\\an2\\pos({width // 2},{_n(y + sp['fs'] * 0.10)})\\q2"
              f"\\fs{_n(sp['fs'])}")
        if not sp["heavy"] and light_family:
            ov += f"\\fn{light_family}\\b0"
        if sp["key"] and look.accent:
            ov += f"\\1c{_c(look.accent)}"
        ov += ("\\fscx118\\fscy118\\alpha&HFF&"
               "\\t(0,90,0.7,\\fscx100\\fscy100\\alpha&H00&)"
               f"\\t({max(91, dur - exit_ms)},{dur},\\alpha&HFF&)")
        events.append(_dialogue(2, w_start, end, "{" + ov + "}" + _ass._ass_escape(sp["text"])))
    return events


# --- Arc -------------------------------------------------------------------------

def _arc_events(look: Look, tokens: list[str], timings: list[dict],
                start: float, end: float, base_fs: int, width: int,
                height: int, sc: float, measure) -> list[str]:
    """The line curves around a slowly turning ring; the key word sits big
    in the middle. Every glyph is placed at the top of the ring and rotated
    around the ring's centre (\\org), which also orients it tangentially."""
    import math
    k = pick_keyword(tokens) if len(tokens) > 2 else None
    arc_idx = [i for i in range(len(tokens)) if i != k]
    fs = int(round(base_fs))
    track = fs * 0.10
    cx, cy = width / 2.0, height / 2.0 + height * 0.04
    glyphs = []                      # (char, word index, advance)
    for n_i, i in enumerate(arc_idx):
        if n_i:
            glyphs.append((" ", i, measure.width(" ", fs) + track))
        for ch in tokens[i]:
            glyphs.append((ch, i, measure.width(ch, fs) + track))
    total = sum(g[2] for g in glyphs)
    r = min(width, height) * 0.30
    if total > 1.5 * math.pi * r:
        r = total / (1.5 * math.pi)
    if r > min(width, height) * 0.44:
        f = (min(width, height) * 0.44) / r
        fs = max(18, int(fs * f))
        r = min(width, height) * 0.44
        glyphs = [(c, w, a * f) for c, w, a in glyphs]
        total *= f
    dur = max(1, int(round((end - start) * 1000)))
    spin = min(28.0, 5.0 * (end - start))
    events: list[str] = []
    # Thin ring just inside the text.
    ring_r = r - fs * 0.22
    events.append(_drawing(
        1, start, end,
        f"\\an7\\pos(0,0)\\1a&HFF&\\3c{_c(look.primary)}\\3a&H90&"
        f"\\bord{_ass._fmt_num(2 * sc)}\\fad(250,250)",
        _ellipse_path(cx, cy, ring_r, ring_r)))
    cum = 0.0
    for ch, w_i, adv in glyphs:
        mid = cum + adv / 2.0
        cum += adv
        if ch == " ":
            continue
        theta = math.degrees((mid - total / 2.0) / r)
        t_start = max(start, float(timings[w_i]["start"]) - 0.04)
        if t_start >= end:
            t_start = start
        d = max(1, int(round((end - t_start) * 1000)))
        off = int(round((t_start - start) * 1000))
        z0 = -theta - spin * off / dur
        ov = (f"\\an2\\pos({_n(cx)},{_n(cy - r)})\\org({_n(cx)},{_n(cy)})\\fs{fs}"
              f"\\frz{_ass._fmt_num(round(z0, 2))}"
              f"\\t(0,{d},\\frz{_ass._fmt_num(round(-theta - spin, 2))})"
              f"\\fad(120,{min(220, max(60, d // 6))})")
        events.append(_dialogue(2, t_start, end, "{" + ov + "}" + _ass._ass_escape(ch)))
    if k is not None:
        k_start = max(start, float(timings[k]["start"]) - 0.04)
        if k_start >= end:
            k_start = start
        d = max(1, int(round((end - k_start) * 1000)))
        kfs = int(round(min(base_fs * look.key_scale, ring_r * 1.6 * 100 / max(1, measure.width(tokens[k], 100)))))
        ov = (f"\\an5\\pos({_n(cx)},{_n(cy)})\\fs{kfs}"
              + (f"\\1c{_c(look.accent)}" if look.accent else "")
              + "\\fscx130\\fscy130\\alpha&HFF&\\t(0,120,0.7,\\fscx100\\fscy100\\alpha&H00&)"
              f"\\t({max(121, d - 200)},{d},\\alpha&HFF&)")
        events.append(_dialogue(3, k_start, end, "{" + ov + "}" + _ass._ass_escape(tokens[k])))
    return events


# --- Floor (3D perspective) ------------------------------------------------------

def _floor_events(look: Look, n: int, tokens: list[str], start: float, end: float,
                  base_fs: int, width: int, height: int) -> list[str]:
    """Text lying on the ground (\\frx) that the camera flies over: it starts
    far and small near the horizon and slides toward the viewer."""
    dur = max(1, int(round((end - start) * 1000)))
    k = pick_keyword(tokens)
    parts = []
    for i, tok in enumerate(tokens):
        esc = _ass._ass_escape(tok)
        if i == k and look.accent:
            esc = "{\\1c" + _c(look.accent) + "}" + esc + "{\\1c" + _c(look.primary) + "}"
        parts.append(esc)
    # Break long lines into balanced rows (same break as the plain text) so
    # the slab never runs off the frame while it grows toward the camera.
    rows = _ass._balanced_breaks(" ".join(tokens), base_fs * 1.6, width).split("\n")
    cuts, acc = set(), 0
    for r in rows[:-1]:
        acc += len(r.split())
        cuts.add(acc)
    text = ""
    for i, part in enumerate(parts):
        text += ("\\N" if i in cuts else (" " if i else "")) + part
    yaw = 6 if n % 2 == 0 else -7
    y0, y1 = height * 0.50, height * 0.74
    return [_dialogue(2, start, end,
                      "{" + f"\\an5\\move({width // 2},{_n(y0)},{width // 2},{_n(y1)},0,{dur})"
                      f"\\q2\\fs{base_fs}\\frx48\\frz{yaw}"
                      f"\\fscx70\\fscy70\\t(0,{dur},1.4,\\fscx125\\fscy125)"
                      f"\\fad(220,260)" + "}" + text)]


# --- Write-on script -----------------------------------------------------------------

def _write_on_events(look: Look, display: str, start: float, end: float,
                     base_fs: int, width: int, height: int, sc: float,
                     measure) -> list[str]:
    """A script that writes itself on, row by row, left to right."""
    rows = _ass._balanced_breaks(display, base_fs, width).split("\n")
    dur = max(1, int(round((end - start) * 1000)))
    write = min(int(dur * 0.55), 70 * len(display))
    row_h = base_fs * 0.95
    glow = (f"\\3a&H90&\\blur{_ass._fmt_num(look.glow * sc)}" if look.glow else "")
    events = []
    total_chars = max(1, sum(len(r) for r in rows))
    t = 0
    for r_i, row in enumerate(rows):
        w = measure.width(row, base_fs)
        x0 = width / 2.0 - w / 2.0 - base_fs * 0.3
        x1 = width / 2.0 + w / 2.0 + base_fs * 0.3
        y = height / 2.0 + (r_i - (len(rows) - 1) / 2.0) * row_h
        span = int(write * len(row) / total_chars)
        ov = (f"\\an5\\pos({width // 2},{_n(y)})\\q2\\fs{base_fs}" + glow
              + f"\\clip({_n(x0)},0,{_n(x0)},{height})"
              f"\\t({t},{t + max(1, span)},\\clip({_n(x0)},0,{_n(x1)},{height}))"
              f"\\t({max(t + span + 1, dur - 320)},{dur},\\alpha&HFF&)")
        events.append(_dialogue(2, start, end, "{" + ov + "}" + _ass._ass_escape(row)))
        t += span
    return events


# --- Gradient background -------------------------------------------------------------

def _hex_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _gradient_events(look: Look, width: int, height: int, duration: float) -> list[str]:
    """Sunset gradient in thin bands + a mountain silhouette + a few stars."""
    import math
    stops = [_hex_rgb(c) for c in look.gradient] or [(0, 0, 0), (0, 0, 0)]
    bands = 64
    events = []
    for b in range(bands):
        t = b / (bands - 1)
        seg = min(len(stops) - 2, int(t * (len(stops) - 1)))
        lt = t * (len(stops) - 1) - seg
        c0, c1 = stops[seg], stops[seg + 1]
        rgb = tuple(int(round(c0[i] + (c1[i] - c0[i]) * lt)) for i in range(3))
        y0 = int(height * b / bands)
        y1 = int(height * (b + 1) / bands) + 1
        events.append(_dialogue(0, 0, duration,
                                _rect(width, height, 0, y0, width, y1,
                                      "#%02X%02X%02X" % rgb)))
    def ridge(base: float, amp: float, phase: float) -> str:
        pts = []
        for i in range(97):
            u = i / 96
            y = height * base - height * amp * (
                0.55 * math.sin(u * 2 * math.pi * 1.3 + phase)
                + 0.35 * math.sin(u * 2 * math.pi * 3.7 + 1.1 + phase)
                + 0.18 * abs(math.sin(u * 2 * math.pi * 9.1 + phase)))
            pts.append((width * u, y))
        return (f"m 0 {height} l " + " l ".join(f"{_n(x)} {_n(y)}" for x, y in pts)
                + f" l {width} {height}")

    # Two ridges: a lighter, hazier one behind and the dark one in front.
    events.append(_drawing(1, 0, duration, "\\an7\\pos(0,0)\\1c" + _c("#5B2A7A")
                           + "\\1a&H30&", ridge(0.74, 0.10, 2.0)))
    events.append(_drawing(1, 0, duration, "\\an7\\pos(0,0)\\1c" + _c("#21123A"),
                           ridge(0.84, 0.09, 0.5)))
    for i in range(28):
        x = (i * 0.6180339 % 1.0) * width
        y = ((i * 0.4142135 + 0.13) % 1.0) * height * 0.55
        s = 2 + (i % 3)
        events.append(_drawing(1, 0, duration,
                               f"\\an7\\pos({_n(x)},{_n(y)})\\1c&HFFFFFF&\\1a&H{0x40 + (i % 4) * 0x20:02X}&",
                               _ellipse_path(0, 0, s, s)))
    return events


# --- Hand-drawn extras for the build layout -------------------------------------------

def _circle_key_events(look: Look, px: float, baseline: float, w: float,
                       fs: float, t0: float, end: float, sc: float) -> list[str]:
    """Red marker ring drawn around the key word (two loose loops), revealed
    left to right as if drawn, then boiling like hand animation."""
    cy = baseline - fs * 0.42
    rx, ry = w / 2 + fs * 0.35, fs * 0.62
    dur = max(1, int(round((end - t0) * 1000)))
    x0, x1 = px - rx - fs * 0.3, px + rx + fs * 0.3
    clip = (f"\\clip({_n(x0)},0,{_n(x0)},4000)"
            f"\\t(0,380,\\clip({_n(x0)},0,{_n(x1)},4000))")
    common = (f"\\an7\\pos(0,0)\\1a&HFF&\\3c{_c('#E8322E')}"
              f"\\bord{_ass._fmt_num(4.5 * sc)}\\blur0.6" + clip
              + f"\\t({max(381, dur - 160)},{dur},\\alpha&HFF&)")
    return [
        _drawing(4, t0, end, common + f"\\org({_n(px)},{_n(cy)})\\frz-3",
                 _ellipse_path(px, cy, rx, ry)),
        _drawing(4, t0, end, common + f"\\org({_n(px)},{_n(cy)})\\frz4",
                 _ellipse_path(px + fs * 0.06, cy + fs * 0.04, rx * 0.97, ry * 0.9)),
    ]


def _doodle_events(n: int, block_left: float, block_right: float, top: float,
                   bottom: float, fs: float, t0: float, end: float, sc: float) -> list[str]:
    s = fs * 0.55
    dur = max(1, int(round((end - t0) * 1000)))
    pop = ("\\fscx40\\fscy40\\alpha&HFF&\\t(0,140,0.7,\\fscx100\\fscy100\\alpha&H00&)"
           f"\\t({max(141, dur - 160)},{dur},\\alpha&HFF&)")
    edge = f"\\3c&H202020&\\bord{_ass._fmt_num(2 * sc)}"
    heart_at = (block_right + s * 0.2, top - s * 0.6) if n % 2 == 0 else (block_left - s * 1.2, top - s * 0.4)
    star_at = (block_left - s * 1.1, bottom - s * 0.2) if n % 2 == 0 else (block_right + s * 0.3, bottom - s * 0.5)
    return [
        _drawing(4, t0, end, f"\\an7\\pos({_n(heart_at[0])},{_n(heart_at[1])})\\1c{_c('#FF5DA2')}"
                 + edge + pop + _boil(dur, seed=n, amp=4), _heart_path(s)),
        _drawing(4, t0 + 0.12, end, f"\\an7\\pos({_n(star_at[0])},{_n(star_at[1])})\\1c{_c('#FFD23F')}"
                 + edge + pop + _boil(dur, seed=n + 1, amp=5), _star_path(s * 1.1)),
    ]


# --- Beat helpers ------------------------------------------------------------------

def _beats_in(beats, t0: float, t1: float, every: int = 2) -> list[float]:
    """Beats inside [t0, t1), keeping one of every `every` (the detector
    often returns double-time; every other beat reads as the pulse)."""
    if not beats:
        return []
    picked = [b for i, b in enumerate(beats) if i % every == 0]
    return [b for b in picked if t0 <= b < t1]


def _beat_pulse(beats, t0: float, t1: float, after_ms: int, scale: int = 108) -> str:
    """Quick scale kick on each pulse beat, once the entrance has settled."""
    out = []
    for b in _beats_in(beats, t0, t1):
        ms = int(round((b - t0) * 1000))
        if ms < after_ms or ms > (t1 - t0) * 1000 - 260:
            continue
        out.append(f"\\t({ms},{ms + 50},\\fscx{scale}\\fscy{scale})"
                   f"\\t({ms + 50},{ms + 230},\\fscx100\\fscy100)")
    return "".join(out[:40])


def _beat_blink(beats, t0: float, t1: float, on_alpha: int, off_alpha: int,
                after_ms: int) -> str:
    """Neon-sign blink: alternate on/off on every pulse beat."""
    out = []
    for i, b in enumerate(_beats_in(beats, t0, t1)):
        ms = int(round((b - t0) * 1000))
        if ms < after_ms:
            continue
        a = off_alpha if i % 2 else on_alpha
        out.append(f"\\t({ms},{ms + 1},\\alpha{_a(a)})")
    return "".join(out[:60])


# --- Kinetic composition (Ed Sheeran / Imagine Dragons) ----------------------------

_ENTRANCES = ("left", "right", "drop", "pop", "spin", "stretch")


def _entrance(kind: str, x: float, y: float, sc: float) -> tuple[str, str]:
    """(position tags, transform tags) for a word entering at (x, y)."""
    d = 90 * sc
    if kind == "left":
        return (f"\\move({_n(x - d * 2.2)},{_n(y)},{_n(x)},{_n(y)},0,220)",
                "\\alpha&HFF&\\t(0,120,\\alpha&H00&)")
    if kind == "right":
        return (f"\\move({_n(x + d * 2.2)},{_n(y)},{_n(x)},{_n(y)},0,220)",
                "\\alpha&HFF&\\t(0,120,\\alpha&H00&)")
    if kind == "drop":
        return (f"\\move({_n(x)},{_n(y - d * 1.6)},{_n(x)},{_n(y)},0,200)",
                "\\fscy120\\t(200,260,\\fscy88)\\t(260,340,\\fscy100)")
    if kind == "spin":
        return (f"\\pos({_n(x)},{_n(y)})",
                "\\frz-80\\fscx40\\fscy40\\t(0,260,0.6,\\frz0\\fscx100\\fscy100)")
    if kind == "stretch":
        return (f"\\pos({_n(x)},{_n(y)})",
                "\\fscx260\\fscy60\\alpha&HFF&\\t(0,220,0.5,\\fscx100\\fscy100\\alpha&H00&)")
    return (f"\\pos({_n(x)},{_n(y)})",
            "\\fscx40\\fscy40\\t(0,150,0.6,\\fscx112\\fscy112)\\t(150,240,\\fscx100\\fscy100)")


def _burst_path(cx: float, cy: float, r0: float, r1: float, rays: int = 14,
                thick: float = 6) -> str:
    """Radiating dashes around a point (the comic 'pow' rays)."""
    import math
    parts = []
    for i in range(rays):
        a = 2 * math.pi * i / rays
        ca, sa = math.cos(a), math.sin(a)
        nx, ny = -sa * thick / 2, ca * thick / 2
        x0, y0 = cx + ca * r0, cy + sa * r0
        x1, y1 = cx + ca * r1, cy + sa * r1
        parts.append(f"m {_n(x0 + nx)} {_n(y0 + ny)} l {_n(x1 + nx)} {_n(y1 + ny)} "
                     f"{_n(x1 - nx)} {_n(y1 - ny)} {_n(x0 - nx)} {_n(y0 - ny)}")
    return " ".join(parts)


def _kinetic_events(look: Look, n: int, tokens: list[str], timings: list[dict],
                    start: float, end: float, base_fs: int, width: int,
                    height: int, sc: float, heavy, script, script_family: str,
                    beats) -> list[str]:
    """Words land one by one as they are sung and build a typographic
    composition: function words small in a script face, content words in
    heavy caps stretched to the block width, the key word huge in an accent
    colour with a burst or a bubble behind it. Each row enters from its own
    direction, the key word kicks on the beat, and the whole composition
    smears out at the end of the line."""
    import hashlib
    palette = look.line_colors or (look.primary,)
    if look.flat_colors and look.card_palettes:
        palette = look.card_palettes[n % len(look.card_palettes)]
    ink, alt, accent = (list(palette) + [look.primary] * 3)[:3]
    k = pick_keyword(tokens)

    def small(i: int) -> bool:
        t = _norm(tokens[i])
        return i != k and (t in _STOPWORDS or len(t) <= 2)

    # Rows: runs of heavy words (≤ 12 chars), a small word gets its own row,
    # the key word always gets its own row.
    rows: list[list[int]] = []
    cur: list[int] = []
    for i in range(len(tokens)):
        if i == k or small(i):
            if cur:
                rows.append(cur)
                cur = []
            rows.append([i])
            continue
        if cur and len(" ".join(tokens[j] for j in cur + [i])) > 12:
            rows.append(cur)
            cur = []
        cur.append(i)
    if cur:
        rows.append(cur)
    # Merge consecutive small words ("en el", "de la") into one script row.
    merged: list[list[int]] = []
    for r in rows:
        if merged and small(r[0]) and all(small(j) for j in merged[-1]) and len(r) == 1:
            merged[-1] = merged[-1] + r
        else:
            merged.append(r)
    rows = merged

    vertical = height > width
    # Long lines split into two columns side by side (Ed Sheeran: "ME / and /
    # MY FRIENDS" on the left, "AT THE / TABLE DOING / shots" on the right);
    # short ones sit left, centre or right, alternating per line.
    if not vertical and len(rows) > 4:
        half = (len(rows) + 1) // 2
        columns = [(rows[:half], width * 0.28), (rows[half:], width * 0.72)]
        block_w = width * 0.38
    else:
        slot = (n % 3) if not vertical else 1
        cx = width * (0.32, 0.5, 0.68)[slot] if len(tokens) > 2 else width * 0.5
        columns = [(rows, cx)]
        block_w = width * (0.80 if vertical else 0.46)

    specs = []
    for col_i, (col_rows, cx) in enumerate(columns):
        col_specs = []
        for r in col_rows:
            text = " ".join(tokens[i] for i in r)
            if all(small(i) for i in r):
                fs = base_fs * 1.25
                col_specs.append(dict(r=r, text=text, fs=fs, kind="script", h=fs * 0.95,
                                      cx=cx, col=col_i))
                continue
            is_key = (k in r) and len(r) == 1
            nat = max(1.0, heavy.width(text, 100))
            fs = 100 * block_w * (1.0 if is_key else 0.90) / nat
            fs = max(base_fs * 0.8, min(base_fs * (5.5 if is_key else 4.2), fs))
            col_specs.append(dict(r=r, text=text, fs=fs, kind="key" if is_key else "heavy",
                                  h=fs * 0.84, cx=cx, col=col_i))
        total = sum(sp["h"] for sp in col_specs)
        limit = height * (0.62 if vertical else 0.84)
        if total > limit:
            f = limit / total
            for sp in col_specs:
                sp["fs"] *= f
                sp["h"] *= f
            total = limit
        y = (height - total) / 2.0
        for sp in col_specs:
            y += sp["h"]
            sp["y"] = y
        specs.extend(col_specs)

    seed = int(hashlib.sha1(f"{n}:{' '.join(tokens)}".encode()).hexdigest()[:8], 16)
    dur_line = max(1, int(round((end - start) * 1000)))
    exit_ms = min(260, max(120, int(dur_line * 0.12)))
    events: list[str] = []
    colours = (ink, alt)
    for r_i, sp in enumerate(specs):
        y = sp["y"]
        first = sp["r"][0]
        w_start = max(start, float(timings[first]["start"]) - 0.05)
        if w_start >= end:
            w_start = start
        dur = max(1, int(round((end - w_start) * 1000)))
        smear = (f"\\t({max(1, dur - exit_ms)},{dur},\\fscx230\\blur{_ass._fmt_num(10 * sc)}"
                 "\\alpha&HFF&)")
        x = sp["cx"]
        if sp["kind"] == "script":
            kind = "pop"
            face = f"\\fn{script_family}\\b1" if script_family else ""
            colour = accent if accent else alt
            tilt = -7 if (seed >> r_i) & 1 else 6
            pos, tr = _entrance(kind, x, y + sp["fs"] * 0.05, sc)
            ov = (f"\\an2{pos}\\q2\\fs{_n(sp['fs'])}{face}\\1c{_c(colour)}\\frz{tilt}{tr}"
                  + smear)
        else:
            kind = _ENTRANCES[(seed // 7 + r_i * 3) % len(_ENTRANCES)]
            colour = accent if sp["kind"] == "key" else colours[r_i % 2]
            pos, tr = _entrance(kind, x, y, sc)
            pulse = (_beat_pulse(beats, w_start, end, after_ms=380, scale=110)
                     if sp["kind"] == "key" else "")
            ov = (f"\\an2{pos}\\q2\\fs{_n(sp['fs'])}\\1c{_c(colour)}{tr}{pulse}" + smear)
        if sp["kind"] == "key" and seed % 4 == 3 and len(sp["text"]) >= 4:
            # Letters tumble in one by one instead of the word landing whole.
            kw = heavy.width(sp["text"], sp["fs"])
            events.extend(_cascade_events(sp["text"], x - kw / 2, y, _n(sp["fs"]),
                                          colour, w_start, end, heavy, sc, beats))
            continue
        events.append(_dialogue(3, w_start, end, "{" + ov + "}" + _ass._ass_escape(sp["text"])))

        if sp["kind"] == "key":
            kw = heavy.width(sp["text"], sp["fs"])
            ky = y - sp["fs"] * 0.40
            deco = seed % 3
            t0 = _deco_start(w_start + 0.12, start, end)
            d2 = max(1, int(round((end - t0) * 1000)))
            fade = f"\\t({max(1, d2 - exit_ms)},{d2},\\alpha&HFF&)"
            if deco == 0:
                # Burst rays that blink on the beat.
                rays = _burst_path(x, ky, kw * 0.58, kw * 0.58 + sp["fs"] * 0.45,
                                   rays=16, thick=5 * sc)
                blink = _beat_blink(beats, t0, end, 0x00, 0xB0, after_ms=200)
                events.append(_drawing(2, t0, end,
                                       f"\\an7\\pos(0,0)\\1c{_c(alt)}\\fscx100\\fscy100"
                                       f"\\alpha&HFF&\\t(0,120,\\alpha&H00&){blink}{fade}", rays))
            elif deco == 1:
                # Speech bubble behind the key word.
                bw, bh = kw + sp["fs"] * 0.7, sp["fs"] * 1.15
                bx, by = x - bw / 2, ky - bh / 2
                events.append(_drawing(2, t0, end,
                                       f"\\an7\\pos({_n(bx)},{_n(by)})\\1c{_c(alt)}"
                                       f"\\frz{-4 if seed & 1 else 4}"
                                       f"\\org({_n(x)},{_n(ky)})"
                                       "\\fscx20\\fscy20\\t(0,180,0.6,\\fscx100\\fscy100)" + fade,
                                       _rounded_rect_path(bw, bh, bh * 0.45,
                                                          tail="left" if seed & 2 else "right")))
            else:
                # Hand-drawn underline swoosh, drawn left to right.
                sw = kw * 1.05
                sx0 = x - sw / 2
                sy = y + sp["fs"] * 0.10
                path = (f"m {_n(sx0)} {_n(sy)} b {_n(sx0 + sw * 0.3)} {_n(sy + 14 * sc)} "
                        f"{_n(sx0 + sw * 0.7)} {_n(sy - 6 * sc)} {_n(sx0 + sw)} {_n(sy + 4 * sc)} "
                        f"l {_n(sx0 + sw)} {_n(sy + 16 * sc)} "
                        f"b {_n(sx0 + sw * 0.7)} {_n(sy + 6 * sc)} {_n(sx0 + sw * 0.3)} "
                        f"{_n(sy + 26 * sc)} {_n(sx0)} {_n(sy + 12 * sc)}")
                clip = (f"\\clip({_n(sx0 - 5)},0,{_n(sx0 - 5)},{height})"
                        f"\\t(0,260,\\clip({_n(sx0 - 5)},0,{_n(sx0 + sw + 10)},{height}))")
                events.append(_drawing(2, t0 + 0.08, end,
                                       f"\\an7\\pos(0,0)\\1c{_c(alt)}" + clip + fade, path))
    return events


# --- Neon signs (Ed Sheeran intro) ----------------------------------------------

def _neon_sign_events(look: Look, n: int, display: str, start: float, end: float,
                      base_fs: int, width: int, height: int, sc: float,
                      measure, script_measure, script_family: str, beats) -> list[str]:
    """A neon sign per line: the unlit glass tube shows faintly first, then
    flickers on; a frame (box, arrow or rays) around it blinks on the beat.
    Lines alternate caps tubes and script tubes."""
    col = look.line_colors[n % len(look.line_colors)] if look.line_colors else look.primary
    frame_col = look.line_colors[(n + 2) % len(look.line_colors)] if look.line_colors else col
    script = (n % 2 == 1) and bool(script_family)
    m = script_measure if script else measure
    fs = int(base_fs * (1.35 if script else 1.0))
    rows = _ass._balanced_breaks(display, fs, width).split("\n")
    tw = max(m.width(r, fs) for r in rows)
    # The break estimate is generic; measure the real face and shrink until
    # the tube (plus its glow and frame) fits the frame — script neon on a
    # vertical short used to run off both sides.
    # Leave room for the haze (outline + blur) on both sides.
    limit = width * 0.82 - 2 * (14 + 26) * sc
    for _ in range(4):
        if tw <= limit:
            break
        fs = max(18, int(fs * limit / tw))
        rows = _ass._balanced_breaks(display, fs, width).split("\n")
        tw = max(m.width(r, fs) for r in rows)
    text = "\\N".join(_ass._ass_escape(r) for r in rows)
    th = len(rows) * fs * 0.95
    cx, cy = width / 2.0, height / 2.0
    dur = max(1, int(round((end - start) * 1000)))
    face = f"\\fn{script_family}" if script else ""
    pos = f"\\an5\\pos({_n(cx)},{_n(cy)})\\q2\\fs{fs}{face}"
    lit = 260                      # ms of unlit glass before it turns on

    def flick(on: int) -> str:
        dim = min(255, on + 0xB0)
        return (f"\\alpha&HFF&\\t({lit},{lit + 1},\\alpha{_a(on)})"
                f"\\t({lit + 70},{lit + 71},\\alpha{_a(dim)})"
                f"\\t({lit + 120},{lit + 121},\\alpha{_a(on)})"
                f"\\t({lit + 220},{lit + 221},\\alpha{_a(dim)})"
                f"\\t({lit + 260},{lit + 261},\\alpha{_a(on)})"
                f"\\t({max(lit + 300, dur - 200)},{dur},\\alpha&HFF&)")

    unlit = (pos + f"\\1a&HFF&\\3c&H8A8A8A&\\3a&H60&\\bord{_ass._fmt_num(1.4 * sc)}"
             f"\\shad0\\t({lit + 1},{lit + 2},\\alpha&HFF&)")
    haze = (pos + f"\\1c{_c(col)}\\3c{_c(col)}\\bord{_ass._fmt_num(14 * sc)}"
            f"\\blur{_ass._fmt_num(26 * sc)}\\shad0" + flick(0x78))
    glow = (pos + f"\\1c{_c(col)}\\3c{_c(col)}\\bord{_ass._fmt_num(5 * sc)}"
            f"\\blur{_ass._fmt_num(9 * sc)}\\shad0" + flick(0x30))
    core = (pos + f"\\1c{_c(look.primary)}\\3c{_c(col)}\\bord{_ass._fmt_num(1.8 * sc)}"
            f"\\blur{_ass._fmt_num(1.2 * sc)}\\shad0" + flick(0))
    events = [_dialogue(1, start, end, "{" + unlit + "}" + text),
              _dialogue(1, start, end, "{" + haze + "}" + text),
              _dialogue(2, start, end, "{" + glow + "}" + text),
              _dialogue(3, start, end, "{" + core + "}" + text)]

    # Frame: a tube outline that blinks on the beat.
    kind = n % 3
    pad = fs * 0.55
    if kind == 0:
        fw, fh = tw + 2 * pad, th + pad * 1.2
        path = _rounded_rect_path(fw, fh, fh * 0.35)
        anchor = f"\\an7\\pos({_n(cx - fw / 2)},{_n(cy - fh / 2)})"
    elif kind == 1:
        # Arrow pointing right around the text.
        fw, fh = tw + 2 * pad, th + pad * 1.2
        hx = fh * 0.55
        x0, y0 = cx - fw / 2, cy - fh / 2
        path = (f"m 0 0 l {_n(fw)} 0 l {_n(fw)} {_n(-fh * 0.25)} l {_n(fw + hx)} {_n(fh / 2)} "
                f"l {_n(fw)} {_n(fh * 1.25)} l {_n(fw)} {_n(fh)} l 0 {_n(fh)}")
        anchor = f"\\an7\\pos({_n(x0)},{_n(y0)})"
    else:
        path = _burst_path(0, 0, tw * 0.62, tw * 0.62 + fs * 0.5, rays=22, thick=4 * sc)
        anchor = f"\\an7\\pos({_n(cx)},{_n(cy)})\\fscy55"
    blink = _beat_blink(beats, start, end, 0x00, 0xD0, after_ms=lit + 300)
    # The frame is an outline only: flicker/blink its OUTLINE alpha (\\3a),
    # never \\alpha, which would also turn the transparent fill opaque.
    frame_tail = (flick(0x00) + blink).replace("\\alpha", "\\3a")
    events.append(_drawing(1, start, end,
                           anchor + f"\\1a&HFF&\\3c{_c(frame_col)}\\bord{_ass._fmt_num(9 * sc)}"
                           f"\\blur{_ass._fmt_num(10 * sc)}\\3a&H60&" + frame_tail, path))
    events.append(_drawing(2, start, end,
                           anchor + f"\\1a&HFF&\\3c{_c('#FFFFFF')}\\bord{_ass._fmt_num(2.2 * sc)}"
                           f"\\blur{_ass._fmt_num(1.5 * sc)}" + frame_tail, path))
    return events


# --- Letter cascade (Imagine Dragons "LEVELS") ------------------------------------

def _cascade_events(word: str, x_left: float, baseline: float, fs: int,
                    colour: str, t0: float, end: float, measure, sc: float,
                    beats=None) -> list[str]:
    """The word's letters tumble in one by one along a diagonal."""
    events = []
    x = x_left
    fs = int(fs)
    for i, ch in enumerate(word):
        w = measure.width(ch, fs)
        ts = t0 + i * 0.06
        if ts >= end:
            ts = t0
        d = max(1, int(round((end - ts) * 1000)))
        sx, sy = x - 120 * sc, baseline - 260 * sc
        ov = (f"\\an1\\move({_n(sx)},{_n(sy)},{_n(x)},{_n(baseline)},0,220)\\fs{fs}"
              f"\\1c{_c(colour)}\\frz{35 - (i % 3) * 20}\\t(0,220,\\frz0)"
              + _beat_pulse(beats, ts, end, after_ms=300, scale=110)
              + f"\\t({max(221, d - 200)},{d},\\fscx230\\alpha&HFF&)")
        events.append(_dialogue(3, ts, end, "{" + ov + "}" + _ass._ass_escape(ch)))
        x += w
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
    beats: list[float] | None = None,
) -> str:
    """Complete ASS document for `look` over `segments`.

    primary_override: operator-picked lyric colour (#RRGGBB) that replaces
    the look's text colour; "" keeps the look's own. White (#FFFFFF) also
    keeps it: it is the wizard's default colour and is persisted on every
    job, so it can't be told apart from "not chosen".
    title_lines: the artist/song title card (ass_render.AssLine list) — drawn
    above everything else so every job keeps a readable title card.
    beats: the song's beat times in seconds (beat_snap.detect_beats). Looks
    with beat_sync kick/blink on them; without beats they simply don't."""
    if (primary_override and re.match(r"^#[0-9a-fA-F]{6}$", primary_override)
            and primary_override.upper() != "#FFFFFF"):
        look = dataclasses.replace(look, primary=primary_override)
    if look.force_case == "original":
        # Script, neon and handwriting faces read as type only in the lyric's
        # own case; forcing caps on them looks like a ransom note.
        case_fn = None
    sc = min(width, height) / 1080.0
    path = font_path(look)
    family, _bold = _ass.font_family(path)
    measure = _Measure(path)
    # The operator's size slider keeps its 0.6-1.5 clamp; the look's own
    # scale multiplies on top so display faces (script, neon) can go big.
    user_scale = max(0.6, min(1.5, float(font_scale or 1.0)))

    def fs_for(text_len: int) -> int:
        return max(18, int(round(_ass.lyric_fontsize(text_len, sc, user_scale)
                                 * look.font_scale)))

    header = _header(width, height, family, fs_for(40), look, sc)

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

    if look.background == "gradient":
        events.extend(_gradient_events(look, width, height, duration))

    if look.layout == "chat":
        fs_chat = fs_for(40)
        events.extend(_chat_events(look, lines, width, height, sc, fs_chat,
                                   measure, duration))
        lines = []          # the conversation owns every line

    second, second_family = None, ""
    if look.extra_fonts and look.layout in ("kinetic",) or look.motion == "neon":
        if look.extra_fonts:
            second = _Measure(font_paths(look)[1])
            second_family = _ass.font_family(font_paths(look)[1])[0]

    light, light_family = None, ""
    if look.layout == "block":
        light = _Measure(font_paths(look)[1]) if look.extra_fonts else measure
        if look.extra_fonts:
            light_family = _ass.font_family(font_paths(look)[1])[0]

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
        base_fs = fs_for(len(display))
        if look.layout == "kinetic":
            timings = _ass._word_timings(display, start, end, seg.get("words"))
            events.extend(_kinetic_events(look, n, tokens, timings, start, end,
                                          base_fs, width, height, sc, measure,
                                          second or measure, second_family,
                                          beats or []))
            continue
        if look.layout in ("build", "block", "arc"):
            timings = _ass._word_timings(display, start, end, seg.get("words"))
            if look.layout == "build":
                events.extend(_build_events(look, n, tokens, timings, start, end,
                                            base_fs, width, height, measure))
            elif look.layout == "block":
                events.extend(_block_events(look, tokens, timings, start, end,
                                            base_fs, width, height, sc,
                                            measure, light, light_family))
            else:
                events.extend(_arc_events(look, tokens, timings, start, end,
                                          base_fs, width, height, sc, measure))
            continue
        if look.layout == "floor":
            events.extend(_floor_events(look, n, tokens, start, end, base_fs,
                                        width, height))
            continue
        if look.motion == "neon":
            if second is not None:
                events.extend(_neon_sign_events(look, n, display, start, end, base_fs,
                                                width, height, sc, measure, second,
                                                second_family, beats or []))
            else:
                events.extend(_neon_events(look, n, display, start, end, base_fs,
                                           width, height, sc))
            continue
        if look.motion == "write_on":
            events.extend(_write_on_events(look, display, start, end, base_fs,
                                           width, height, sc, measure))
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
        if look.motion == "boil":
            body = _ass._ass_escape(_ass._balanced_breaks(display, base_fs, width))
            ink = (f"\\1c{_c(look.line_colors[n % len(look.line_colors)])}"
                   if look.line_colors else "")
            ex = min(260, max(120, int(dur_ms * 0.12)))
            # Ink grows in, boils like hand animation, and smears out sideways
            # into the next line (Shakira "Dai Dai").
            text = ("{" + f"\\an5\\pos({width // 2},{height // 2})\\q2\\fs{base_fs}"
                    + ink + glow
                    + "\\fscx70\\fscy70\\alpha&HFF&"
                    "\\t(0,160,0.6,\\fscx100\\fscy100\\alpha&H00&)"
                    + _boil(dur_ms - ex, seed=n)
                    + f"\\t({dur_ms - ex},{dur_ms},\\fscx320\\fscy115"
                    f"\\blur{_ass._fmt_num(14 * sc)}\\alpha&HFF&)" + "}" + body)
            events.append(_dialogue(2, start, end, text))
            continue
        text = ("{" + f"\\fs{base_fs}" + glow + _line_motion(look, dur_ms) + "}"
                + _ass._ass_escape(display))
        events.append(_dialogue(2, start, end, text))

    if title_lines:
        # The title card sits on the background, not on the look's own
        # surfaces (chat bubbles, cards): a dark look colour would vanish, so
        # it falls back to white, and always keeps a readable outline.
        r, g, b = _hex_rgb(look.primary)
        dark = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0 < 0.45
        doc = _ass.build_ass(width=width, height=height, font_name=family,
                             base_fontsize=fs_for(40),
                             outline=max(look.outline, 2.0) * sc,
                             shadow=max(look.shadow, 2.0) * sc,
                             lines=list(title_lines), bold=False,
                             primary_color="#FFFFFF" if dark else look.primary)
        for ln in doc.splitlines():
            if ln.startswith("Dialogue: "):
                # Above the cards (0), the lyrics (2) AND the letterbox bars
                # (5): a low title card must never end up behind a bar.
                events.append("Dialogue: 6," + ln.split(",", 1)[1])

    return header + "\n" + "\n".join(e for e in events if e) + "\n"
