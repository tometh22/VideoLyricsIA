"""Poster typography for the YouTube thumbnail of a lyric-look job.

The intro title card is designed to sit quietly over the first seconds of a
video; enlarged into a still it reads as "artist / song" in a centered stack.
A thumbnail has to sell the song at 320 px wide, so jobs with a lyric look get
a poster composition instead, built from the SAME effective look the video
uses (lyric_look_runtime.resolve_for_render: genre skin, recipe variant and
operator font/colour overrides):

* the look's own font file(s) and palette (ink + accent on the key word),
* a layout chosen from the look's composition:
    - "keyword"  small tracked lead + huge key word + ruled artist (Cine),
    - "block"    rows stretched to one width, light/heavy, accent row (Bloque),
    - "arc"      title bent around a ring, artist tracked inside (Arco),
    - "script"   big handwritten/script title with a soft glow (Romántico…),
    - "stack"    everything else: big title, key word in the accent colour,
                 tracked artist underneath,
* a scrim, halo and drop shadow (or the look's own hard shadow / outline /
  neon glow) so the type stays legible on any background.

Everything is auto-fitted: long titles wrap and shrink, and a final pass
scales the whole text block into the safe area so nothing ever leaves the
frame. Text a look's font can't draw (Cyrillic on a Latin-only face…) falls
back to Montserrat; when that can't draw it either (CJK, Arabic…) the caller
keeps the libass title card exactly as before this module existed.

Pure Pillow: the caller hands in the already graded background frame.
"""
from __future__ import annotations

import dataclasses
import itertools
import math
import os
import re
import unicodedata
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageStat

import lyric_looks as L

# Composition happens at 1080p (the scale every look's metrics are tuned
# for) and is downsampled to the delivered thumbnail size at the end.
W, H = 1920, 1080
OUT_SIZE = (1280, 720)
# Text never goes outside this box (fractions of the frame).
SAFE = (0.055, 0.075, 0.945, 0.925)

FALLBACK_FONT = "Montserrat-ExtraBold.ttf"
ARTIST_SANS = "Montserrat-Bold.ttf"

LAYOUTS = ("stack", "keyword", "block", "arc", "script")


class PosterUnsupported(Exception):
    """The text can't be drawn with the look's font nor the fallback."""


# --- Fonts & measuring ----------------------------------------------------------

def _fonts_dir() -> str:
    return L._FONTS_DIR


def _path(name: str) -> str:
    return name if os.path.isabs(name) else os.path.join(_fonts_dir(), name)


@lru_cache(maxsize=512)
def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, max(1, int(size)))


def _glyph_raster(path: str, ch: str) -> bytes:
    f = _font(path, 48)
    im = Image.new("L", (112, 96))
    ImageDraw.Draw(im).text((24, 12), ch, font=f, fill=255)
    return im.tobytes()


@lru_cache(maxsize=64)
def _notdef(path: str) -> bytes:
    # U+10FFFD is a private-use noncharacter no music font maps: it renders
    # the font's .notdef glyph (box / blank), the same thing a missing
    # letter would render as.
    return _glyph_raster(path, "\U0010FFFD")


@lru_cache(maxsize=4096)
def _has_glyph(path: str, ch: str) -> bool:
    return _glyph_raster(path, ch) != _notdef(path)


def covers(path: str, text: str) -> bool:
    """True when the font file draws every visible character of `text`."""
    for ch in set(text or ""):
        if ch.isspace() or unicodedata.category(ch)[0] in ("C", "Z"):
            continue
        if not _has_glyph(path, ch):
            return False
    return True


def _pick_font(path: str, text: str) -> str:
    if covers(path, text):
        return path
    fb = _path(FALLBACK_FONT)
    if covers(fb, text):
        return fb
    raise PosterUnsupported(f"no poster font draws {text!r}")


@lru_cache(maxsize=64)
def _cap_em(path: str) -> float:
    bb = _font(path, 1000).getbbox("H")
    return max(0.2, (bb[3] - bb[1]) / 1000.0)


def _norm(path: str) -> float:
    """Size multiplier so faces with small caps (scripts, neon) read as big
    as Montserrat at the same nominal size."""
    return max(0.85, min(1.9, 0.70 / _cap_em(path)))


def _norm_script(path: str) -> float:
    """Scripts have tiny caps AND a thin stroke: let them go bigger."""
    return max(0.85, min(2.5, 0.70 / _cap_em(path)))


@lru_cache(maxsize=4096)
def _width100(path: str, text: str, track: float) -> float:
    f = _font(path, 100)
    if not text:
        return 0.0
    if not track:
        return float(f.getlength(text))
    return sum(f.getlength(c) for c in text) + track * 100 * (len(text) - 1)


def _width(path: str, size: float, text: str, track: float = 0.0) -> float:
    return _width100(path, text, track) * size / 100.0


def _fit_size(path: str, text: str, max_w: float, max_size: float,
              track: float = 0.0) -> float:
    w = _width100(path, text, track)
    if w <= 0:
        return max_size
    return min(max_size, 0.985 * 100.0 * max_w / w)


def _splits(words: list[str], n: int):
    if n == 1:
        yield [" ".join(words)]
        return
    for cuts in itertools.combinations(range(1, len(words)), n - 1):
        bounds = (0,) + cuts + (len(words),)
        yield [" ".join(words[a:b]) for a, b in zip(bounds, bounds[1:])]


def wrap_fit(text: str, path: str, *, max_w: float, max_h: float,
             max_size: float, max_lines: int = 3, track: float = 0.0,
             line_h: float = 1.15, forced: list[str] | None = None
             ) -> tuple[float, list[str]]:
    """(size, lines) — the biggest size at which `text` fits max_w × max_h in
    at most `max_lines` balanced lines. Fewer lines win near-ties."""
    if forced:
        lines = [ln for ln in forced if ln.strip()]
        size = min(_fit_size(path, ln, max_w, max_size, track) for ln in lines)
        return min(size, max_h / (len(lines) * line_h)), lines
    words = text.split()
    if len(words) > 24:   # keep the search small; a 25-word title is a sentence
        words = words[:23] + [" ".join(words[23:])]
    best: tuple[float, float, list[str]] | None = None
    for n in range(1, min(max_lines, len(words)) + 1):
        for lines in _splits(words, n):
            size = min(_fit_size(path, ln, max_w, max_size, track) for ln in lines)
            size = min(size, max_h / (n * line_h))
            score = size * (0.92 ** (n - 1))
            if best is None or score > best[0]:
                best = (score, size, lines)
    if best is None:
        return max_size, [text]
    return best[1], best[2]


# --- Colours ----------------------------------------------------------------------

def _rgb(hex_color: str) -> tuple[int, int, int]:
    return L._hex_rgb(hex_color)


def _lum(rgb: tuple[int, int, int]) -> float:
    r, g, b = (v / 255.0 for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _mix(a, b, t: float) -> tuple[int, int, int]:
    return tuple(int(round(x + (y - x) * t)) for x, y in zip(a, b))


def _valid_hex(c: str) -> bool:
    c = (c or "").strip()
    return len(c) == 7 and c.startswith("#") and all(
        ch in "0123456789abcdefABCDEF" for ch in c[1:])


# --- Style --------------------------------------------------------------------------

@dataclasses.dataclass
class Style:
    layout: str
    title_font: str
    key_font: str          # the big key word (keyword layout)
    light_font: str        # light rows (block layout)
    artist_font: str
    ink: tuple[int, int, int]
    accent: tuple[int, int, int] | None
    artist_color: tuple[int, int, int]
    title_case: str        # "upper" | "lower" | "original"
    glow: tuple[tuple[int, int, int], float, float] | None = None  # colour, radius, strength
    neon: bool = False
    stroke: tuple[float, tuple[int, int, int]] | None = None  # px per 100 px of size, colour
    hard_shadow: tuple[float, tuple[int, int, int]] | None = None
    scrim: str = "center"  # "left" | "center" | "none"
    ref_size: float = 0.0  # biggest row drawn (scales the hard shadow)
    subtitle: str = ""     # "(En Vivo)", "feat. X"… split off the title


def _font_roles(font_file: str) -> tuple[str, ...]:
    try:
        import lyric_recipes as R
        fid = R._FONT_BY_FILE.get(os.path.basename(font_file))
        return R.FONTS[fid][5] if fid else ()
    except Exception:
        return ()


def layout_for(look: L.Look, title_font_file: str = "") -> str:
    """The poster composition for a resolved look."""
    if look.layout == "arc":
        return "arc"
    if look.layout == "block":
        return "block"
    if look.layout == "keyword":
        return "keyword"
    roles = _font_roles(title_font_file or look.font_file)
    if look.motion == "write_on" or (roles and roles[0] in ("script", "hand")):
        return "script"
    return "stack"


def style_for(look: L.Look, *, song_font_path: str | None = None,
              artist_font_path: str | None = None,
              primary_override: str = "") -> Style:
    title_font = song_font_path or L.font_path(look)
    layout = layout_for(look, os.path.basename(title_font))
    ink_hex = look.primary
    if (_valid_hex(primary_override) and primary_override.upper() != "#FFFFFF"):
        ink_hex = primary_override
    accent_hex = look.accent or ""
    if look.line_colors and (look.layout in L.EDITORIAL_LAYOUTS
                             or look.layout == "kinetic"):
        ink_hex = look.line_colors[0]
        if len(look.line_colors) > 2:
            accent_hex = look.line_colors[2]
    elif look.card_palettes and look.background == "flat":
        ink_hex, _alt, accent_hex = look.card_palettes[0][:3]
    neon = look.motion == "neon"
    if neon and look.line_colors:
        accent_hex = look.line_colors[0]
    ink = _rgb(ink_hex) if _valid_hex(ink_hex) else (255, 255, 255)
    painted = look.background in ("flat", "gradient")
    # The type sits on a photo darkened by the scrim (or the look's own
    # card): a dark ink (Chat's near-black, Diario's violet) disappears.
    if _lum(ink) < 0.45 and not (painted and look.flat_colors
                                 and _lum(_rgb(look.flat_colors[0])) > 0.6):
        ink = (255, 255, 255)
    accent = _rgb(accent_hex) if _valid_hex(accent_hex) else None
    if accent is not None and (_lum(accent) < 0.16 or accent == ink):
        accent = None

    second = ""
    if look.layout == "block" or look.motion == "neon":
        second = look.extra_fonts[0] if look.extra_fonts else ""
    key_font = _path(look.key_font) if (look.key_font and layout == "keyword") else title_font
    light_font = _path(second) if (layout == "block" and second) else title_font

    if look.force_case == "lower":
        case = "lower"
    elif look.force_case == "original" or layout == "script":
        case = "original"
    else:
        case = "upper"

    if artist_font_path:
        artist_font = artist_font_path
    elif layout == "script" or neon:
        artist_font = _path(ARTIST_SANS)
    elif layout == "block":
        artist_font = light_font
    else:
        artist_font = title_font
    artist_color = ink

    glow = None
    if neon:
        tube = accent or ink
        glow = (tube, 22.0, 2.4)
        artist_color = _mix(_rgb(look.line_colors[1]) if len(look.line_colors) > 1
                            else tube, (255, 255, 255), 0.35)
        ink = _mix(tube, (255, 255, 255), 0.72)
    elif look.glow or layout == "script":
        oc = _rgb(look.outline_color) if _valid_hex(look.outline_color) else (0, 0, 0)
        if _lum(oc) > 0.45:
            g = oc
        elif accent is not None and layout != "script":
            g = ink
        else:
            g = accent or ink
        glow = (g, 26.0 if layout == "script" else 16.0,
                1.5 if layout == "script" else 0.85)
        if layout == "script":
            artist_color = _mix(ink, g, 0.18)

    stroke = None
    hard_shadow = None
    if not neon and look.outline >= 2.5 and _valid_hex(look.outline_color):
        stroke = (look.outline * 0.9, _rgb(look.outline_color))
    if (not neon and look.shadow > 0 and look.shadow_alpha <= 0x30
            and _valid_hex(look.shadow_color)):
        hard_shadow = (look.shadow * 0.9, _rgb(look.shadow_color))

    scrim = "none" if painted else ("left" if layout in ("keyword", "block") else "center")
    return Style(layout=layout, title_font=title_font, key_font=key_font,
                 light_font=light_font, artist_font=artist_font, ink=ink,
                 accent=accent, artist_color=artist_color, title_case=case,
                 glow=glow, neon=neon, stroke=stroke, hard_shadow=hard_shadow,
                 scrim=scrim)


# --- Rows ---------------------------------------------------------------------------

@dataclasses.dataclass
class Row:
    spans: list[tuple[str, tuple[int, int, int]]]
    font: str
    size: float
    track: float = 0.0      # em
    gap_after: float = 0.0  # px
    rule: bool = False      # short rule before the text (Cine artist line)
    alpha: int = 255

    @property
    def text(self) -> str:
        return "".join(t for t, _ in self.spans)


_TRAILING_PAREN = re.compile(r"^(.*?\S)\s*[(\[]([^()\[\]]+)[)\]]\s*$")


def split_title(title: str) -> tuple[str, str]:
    """("Matador", "En Vivo") from "Matador (En Vivo)" or "Matador - En Vivo".
    The qualifier becomes a small subtitle so the song name itself stays big."""
    t = " ".join((title or "").split())
    m = _TRAILING_PAREN.match(t)
    if m and len(m.group(1).strip(" -")) >= 2:
        return m.group(1).strip(" -"), m.group(2).strip()
    if " - " in t:
        head, tail = (x.strip() for x in t.split(" - ", 1))
        if len(head) >= 2 and tail:
            return head, tail
    return t, ""


def _case(text: str, case: str) -> str:
    if case == "upper":
        return text.upper()
    if case == "lower":
        return text.lower()
    return text


def _row_width(row: Row) -> float:
    w = _width(row.font, row.size, row.text, row.track)
    if row.rule:
        w += row.size * 2.6
    return w


def _ink_box(row: Row) -> tuple[int, int]:
    f = _font(row.font, int(round(row.size)))
    bb = f.getbbox(row.text or "H")
    return bb[1], bb[3]


def _draw_row(d: ImageDraw.ImageDraw, row: Row, x: float, top: float,
              stroke: tuple[float, tuple] | None) -> None:
    size = int(round(row.size))
    f = _font(row.font, size)
    y0, _y1 = _ink_box(row)
    y = top - y0
    sw = 0
    sf = None
    if stroke:
        sw = max(1, int(round(stroke[0] * size / 100.0)))
        sf = stroke[1] + (row.alpha,)
    if row.rule:
        cy = top + (_y1 - y0) * 0.5
        color = row.spans[0][1] + (int(row.alpha * 0.85),)
        d.line((x, cy, x + size * 1.9, cy), fill=color, width=max(2, size // 16))
        x += size * 2.6
    track = row.track * size
    for text, color in row.spans:
        fill = color + (row.alpha,)
        if not track:
            d.text((x, y), text, font=f, fill=fill, stroke_width=sw, stroke_fill=sf)
            x += f.getlength(text)
            continue
        for ch in text:
            d.text((x, y), ch, font=f, fill=fill, stroke_width=sw, stroke_fill=sf)
            x += f.getlength(ch) + track


def _stack_height(rows: list[Row]) -> float:
    h = 0.0
    for i, r in enumerate(rows):
        a, b = _ink_box(r)
        h += b - a
        if i < len(rows) - 1:
            h += r.gap_after
    return h


def _draw_stack(layer: Image.Image, rows: list[Row], *, align: str, x: float,
                cy: float, stroke=None, st: Style | None = None) -> None:
    if st is not None:
        st.ref_size = max([st.ref_size] + [r.size for r in rows])
    d = ImageDraw.Draw(layer)
    top = cy - _stack_height(rows) / 2
    for r in rows:
        a, b = _ink_box(r)
        rx = x - _row_width(r) / 2 if align == "center" else x
        _draw_row(d, r, rx, top, stroke)
        top += (b - a) + r.gap_after


def _keyword_spans(lines: list[str], st: Style) -> list[list[tuple[str, tuple]]]:
    """Title lines as coloured spans: the last word in the accent colour."""
    out = [[(ln, st.ink)] for ln in lines]
    words = " ".join(lines).split()
    if st.accent is None or len(words) < 2:
        return out
    last = lines[-1]
    head, _sep, tail = last.rpartition(" ")
    if head:
        out[-1] = [(head + " ", st.ink), (tail, st.accent)]
    else:
        out[-1] = [(last, st.accent)]
    return out


# --- Layouts ------------------------------------------------------------------------

def _artist_rows(artist: str, st: Style, *, max_w: float, max_size: float,
                 min_size: float, track: float, color=None, rule=False,
                 font: str | None = None) -> list[Row]:
    if not artist:
        return []
    font = font or st.artist_font
    eff_w = max_w - (max_size * 2.6 if rule else 0)
    # One line whenever it stays readable; long names (featurings) wrap.
    size, lines = _fit_size(font, artist, eff_w, max_size, track), [artist]
    if size < min_size and len(artist.split()) > 1:
        size, lines = wrap_fit(artist, font, max_w=eff_w, max_h=max_size * 2.6,
                               max_size=max_size, max_lines=2, track=track)
    rows = [Row([(ln, color or st.artist_color)], font, max(size, min_size * 0.75),
                track=track, gap_after=size * 0.35, rule=rule and i == 0, alpha=240)
            for i, ln in enumerate(lines)]
    rows[-1].gap_after = 0
    return rows


def _sub_rows(st: Style, *, max_w: float, max_size: float, gap: float,
              font: str | None = None) -> list[Row]:
    if not st.subtitle:
        return []
    font = font or st.artist_font
    size, lines = _fit_size(font, st.subtitle, max_w, max_size, 0.16), [st.subtitle]
    if size < 26 and len(st.subtitle.split()) > 1:
        size, lines = wrap_fit(st.subtitle, font, max_w=max_w, max_h=max_size * 2.6,
                               max_size=max_size, max_lines=2, track=0.16)
    rows = [Row([(ln, st.artist_color)], font, max(18.0, size), track=0.16,
                gap_after=size * 0.4, alpha=215) for ln in lines]
    rows[-1].gap_after = gap
    return rows


def _layout_stack(layer, artist, title, st: Style, song_lines):
    tf = st.title_font
    n = _norm(tf)
    size, lines = wrap_fit(title, tf, max_w=0.80 * W, max_h=0.50 * H,
                           max_size=205 * n, max_lines=3, line_h=1.12,
                           forced=song_lines)
    rows = [Row(sp, tf, size, gap_after=size * 0.10)
            for sp in _keyword_spans(lines, st)]
    if rows:
        rows[-1].gap_after = max(size * 0.30, 34)
    a_max = max(34.0, min(60.0, size * 0.34)) * (_norm(st.artist_font) if st.neon else 1)
    rows += _sub_rows(st, max_w=0.70 * W, max_size=a_max * 0.8, gap=a_max * 0.55)
    rows += _artist_rows(artist, st, max_w=0.70 * W, max_size=a_max, min_size=24,
                         track=0.20)
    if not rows:
        return
    _draw_stack(layer, rows, align="center", x=W / 2, cy=H * 0.5, stroke=st.stroke, st=st)


def _layout_script(layer, artist, title, st: Style, song_lines):
    tf = st.title_font
    n = _norm(tf)
    size, lines = wrap_fit(title, tf, max_w=0.82 * W, max_h=0.52 * H,
                           max_size=250 * max(n, _norm_script(tf)), max_lines=2,
                           line_h=1.05,
                           forced=song_lines)
    rows = [Row(sp, tf, size, gap_after=size * 0.06)
            for sp in [[(ln, st.ink)] for ln in lines]]
    if rows:
        rows[-1].gap_after = max(size * 0.22, 36)
    rows += _sub_rows(st, max_w=0.62 * W, max_size=36, gap=26)
    rows += _artist_rows(artist, st, max_w=0.62 * W, max_size=46, min_size=24,
                         track=0.36)
    if rows:
        _draw_stack(layer, rows, align="center", x=W / 2, cy=H * 0.5,
                    stroke=st.stroke, st=st)


def _layout_keyword(layer, artist, title, st: Style, song_lines):
    x0 = 0.085 * W
    col = 0.52 * W
    if song_lines and len(song_lines) > 1:
        lead_txt, key_txt = " ".join(song_lines[:-1]), song_lines[-1]
    else:
        words = title.split()
        # The key word is the last one; a tiny one ("YO", "Ü") takes the
        # word before it along.
        k = 2 if len(words) > 2 and len(words[-1]) < 3 else 1
        lead_txt, key_txt = " ".join(words[:-k]), " ".join(words[-k:])
    kf = st.key_font
    key_size = _fit_size(kf, key_txt, col, 270 * _norm(kf))
    if not key_txt or key_size < 130:
        # A long key word would come out small: plain left-aligned title.
        tf = st.title_font
        size, lines = wrap_fit(title, tf, max_w=col, max_h=0.52 * H,
                               max_size=180 * _norm(tf), max_lines=3,
                               forced=song_lines)
        rows = [Row(sp, tf, size, gap_after=size * 0.14)
                for sp in _keyword_spans(lines, st)]
    else:
        rows = []
        if lead_txt:
            lsize, llines = wrap_fit(lead_txt, st.title_font, max_w=col,
                                     max_h=key_size * 0.9,
                                     max_size=min(96.0, key_size * 0.40),
                                     max_lines=2, track=0.22)
            rows += [Row([(ln, st.ink)], st.title_font, lsize, track=0.22,
                         gap_after=lsize * 0.45) for ln in llines]
        rows.append(Row([(key_txt, st.ink)], kf, key_size, gap_after=0))
    if rows:
        rows[-1].gap_after = max(rows[-1].size * 0.26, 40)
    rows += _sub_rows(st, max_w=col, max_size=38, gap=30)
    rows += _artist_rows(artist, st, max_w=col, max_size=44, min_size=26,
                         track=0.30, rule=True)
    if rows:
        _draw_stack(layer, rows, align="left", x=x0, cy=H * 0.5, stroke=st.stroke, st=st)


def _chunk(words: list[str], target: int) -> list[str]:
    rows, cur = [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > target:
            rows.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        rows.append(cur)
    return rows


def _layout_block(layer, artist, title, st: Style, song_lines):
    x0 = 0.08 * W
    track = 0.03
    if song_lines:
        trows = [ln for ln in song_lines if ln.strip()]
    else:
        words = title.split()
        target = max(7, math.ceil(len(title) / 4))
        trows = _chunk(words, target) if words else []
    cap_heavy = 0.21 * H / _cap_em(st.title_font)
    cap_light = 0.10 * H / _cap_em(st.light_font)
    gap = 0.024 * H
    width = 0.31 * W
    rows: list[Row] = []
    for _ in range(4):
        rows = []
        if artist:
            asz = _fit_size(st.light_font, artist, width, cap_light, track)
            if asz < 48 and len(artist.split()) > 1:
                _sz, alines = wrap_fit(artist, st.light_font, max_w=width, max_h=H,
                                       max_size=cap_light, max_lines=2, track=track)
                for ln in alines:
                    rows.append(Row([(ln, st.ink)], st.light_font,
                                    _fit_size(st.light_font, ln, width, cap_light, track),
                                    track=track, gap_after=gap, alpha=235))
            else:
                rows.append(Row([(artist, st.ink)], st.light_font, asz,
                                track=track, gap_after=gap, alpha=235))
        for i, tr in enumerate(trows):
            color = st.accent if (st.accent is not None and i == len(trows) - 1
                                  and len(trows) > 1) else st.ink
            rows.append(Row([(tr, color)], st.title_font,
                            _fit_size(st.title_font, tr, width, cap_heavy, track),
                            track=track, gap_after=gap))
        rows += _sub_rows(st, max_w=width, max_size=cap_light * 0.55, gap=gap,
                          font=st.light_font)
        if not rows:
            return
        rows[-1].gap_after = 0
        h = _stack_height(rows)
        if h <= 0.80 * H:
            break
        width *= 0.80 * H / h
    _draw_stack(layer, rows, align="left", x=x0, cy=H * 0.5, stroke=st.stroke, st=st)


def _layout_arc(layer, artist, title, st: Style, song_lines) -> bool:
    """Title bent over a ring. False when the title is too long to read on
    the arc (caller falls back to the stack)."""
    if song_lines:
        title = " ".join(song_lines)
    cx, cy, r = W / 2, H * 0.52, H * 0.305
    tf = st.title_font
    track = 0.09
    span = 3.4  # radians available on the top of the ring (~195°)
    size = _fit_size(tf, title, r * span, 112 * _norm(tf), track)
    if size < 62 or len(title) > 40:
        return False
    size = int(round(size))
    st.ref_size = size
    f = _font(tf, size)
    spans = _keyword_spans([title], st)[0]
    chars = [(c, col) for txt, col in spans for c in txt]
    tr = track * size
    widths = [f.getlength(c) + tr for c, _ in chars]
    total = sum(widths) - tr
    a = -math.pi / 2 - total / r / 2
    box = int(size * 2.6)
    sw = max(1, int(round(st.stroke[0] * size / 100))) if st.stroke else 0
    for (c, col), wd in zip(chars, widths):
        mid = a + (wd - tr) / 2 / r
        x, y = cx + r * math.cos(mid), cy + r * math.sin(mid)
        g = Image.new("RGBA", (box, box), (0, 0, 0, 0))
        ImageDraw.Draw(g).text((box / 2, box / 2), c, font=f, fill=col + (255,),
                               anchor="ms", stroke_width=sw,
                               stroke_fill=(st.stroke[1] + (255,)) if st.stroke else None)
        g = g.rotate(-math.degrees(mid + math.pi / 2), resample=Image.BICUBIC)
        layer.alpha_composite(g, (int(round(x - box / 2)), int(round(y - box / 2))))
        a += wd / r
    ring = r - size * 0.62
    d = ImageDraw.Draw(layer)
    d.ellipse((cx - ring, cy - ring, cx + ring, cy + ring),
              outline=st.ink + (150,), width=3)
    rows = _artist_rows(artist, st, max_w=ring * 2 * 0.82, max_size=50,
                        min_size=20, track=0.28)
    if rows:
        rows[-1].gap_after = 22
    rows += _sub_rows(st, max_w=ring * 2 * 0.72, max_size=28, gap=0)
    if rows:
        _draw_stack(layer, rows, align="center", x=cx, cy=cy - ring * 0.12,
                    stroke=None)
    return True


# --- Effects ------------------------------------------------------------------------

def _fit_into_safe(layer: Image.Image) -> Image.Image:
    """Scale the text block down (around its own centre, then nudged inside)
    when anything would leave the safe area."""
    bbox = layer.getchannel("A").getbbox()
    if not bbox:
        return layer
    sx0, sy0, sx1, sy1 = (int(SAFE[0] * W), int(SAFE[1] * H),
                          int(SAFE[2] * W), int(SAFE[3] * H))
    x0, y0, x1, y1 = bbox
    if x0 >= sx0 and y0 >= sy0 and x1 <= sx1 and y1 <= sy1:
        return layer
    bw, bh = x1 - x0, y1 - y0
    k = min(1.0, (sx1 - sx0) / bw, (sy1 - sy0) / bh)
    crop = layer.crop(bbox)
    if k < 1.0:
        crop = crop.resize((max(1, int(bw * k)), max(1, int(bh * k))), Image.LANCZOS)
    cw, ch = crop.size
    nx = min(max((x0 + x1) / 2 - cw / 2, sx0), sx1 - cw)
    ny = min(max((y0 + y1) / 2 - ch / 2, sy0), sy1 - ch)
    out = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    out.alpha_composite(crop, (int(round(nx)), int(round(ny))))
    return out


def _blur_alpha(alpha: Image.Image, radius: float) -> Image.Image:
    """Gaussian blur of a mask; big radii are done at a quarter scale."""
    if radius <= 12:
        return alpha.filter(ImageFilter.GaussianBlur(radius))
    small = alpha.resize((W // 4, H // 4), Image.BILINEAR)
    small = small.filter(ImageFilter.GaussianBlur(radius / 4))
    return small.resize((W, H), Image.BILINEAR)


def _tint(alpha: Image.Image, color, strength: float = 1.0,
          offset=(0, 0)) -> Image.Image:
    a = alpha.point(lambda v: min(255, int(v * strength)))
    out = Image.new("RGBA", (W, H), tuple(color) + (0,))
    out.putalpha(a)
    if offset != (0, 0):
        shifted = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        shifted.alpha_composite(out, (int(offset[0]), int(offset[1])))
        return shifted
    return out


def _left_scrim(strength: float, width: float = 0.66) -> Image.Image:
    ramp = Image.new("L", (256, 1))
    ramp.putdata([int(255 * strength * max(0.0, 1 - x / (255 * width)) ** 1.35)
                  for x in range(256)])
    a = ramp.resize((W, H), Image.BILINEAR)
    out = Image.new("RGBA", (W, H), (0, 0, 0, 255))
    out.putalpha(a)
    return out


def _vignette(strength: float) -> Image.Image:
    a = Image.radial_gradient("L").resize((W, H), Image.BILINEAR)
    a = a.point(lambda v: int(min(255, v * 1.15) * strength))
    out = Image.new("RGBA", (W, H), (0, 0, 0, 255))
    out.putalpha(a)
    return out


def _region_luma(bg: Image.Image, bbox) -> float:
    if not bbox:
        return 0.0
    stat = ImageStat.Stat(bg.convert("L").crop(bbox))
    return stat.mean[0] / 255.0


def text_layer(look: L.Look, *, artist: str, title: str,
               song_lines: list[str] | None = None,
               song_font_path: str | None = None,
               artist_font_path: str | None = None,
               primary_override: str = "") -> tuple[Image.Image, Style, str]:
    """(RGBA W×H layer holding only the type, resolved Style, layout used)."""
    st = style_for(look, song_font_path=song_font_path,
                   artist_font_path=artist_font_path,
                   primary_override=primary_override)
    artist = " ".join(unicodedata.normalize("NFC", artist or "").split()).upper()
    title = " ".join(unicodedata.normalize("NFC", title or "").split())
    if not song_lines:
        title, sub = split_title(title)
        st.subtitle = sub.upper()
    title = _case(title, st.title_case)
    song_lines = ([" ".join(unicodedata.normalize("NFC", _case(ln, st.title_case)).split())
                   for ln in song_lines if ln.strip()] if song_lines else None) or None
    all_title = " ".join(song_lines) if song_lines else title
    # Fonts that can't draw the text fall back to Montserrat.
    st.title_font = _pick_font(st.title_font, all_title)
    st.key_font = _pick_font(st.key_font, all_title)
    st.light_font = _pick_font(st.light_font, artist + st.subtitle)
    st.artist_font = _pick_font(st.artist_font, artist + st.subtitle)

    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    layout = st.layout
    if layout == "arc" and not _layout_arc(layer, artist, title, st, song_lines):
        layout = "stack"
    {"stack": _layout_stack, "script": _layout_script, "keyword": _layout_keyword,
     "block": _layout_block}.get(layout, lambda *a: None)(
        layer, artist, title, st, song_lines)
    return _fit_into_safe(layer), st, layout


def compose(bg: Image.Image, look: L.Look, *, artist: str, title: str,
            song_lines: list[str] | None = None,
            song_font_path: str | None = None,
            artist_font_path: str | None = None,
            primary_override: str = "") -> Image.Image:
    """The poster thumbnail (RGB, OUT_SIZE) for `look` over `bg`."""
    layer, st, layout = text_layer(
        look, artist=artist, title=title, song_lines=song_lines,
        song_font_path=song_font_path, artist_font_path=artist_font_path,
        primary_override=primary_override)
    base = bg.convert("RGB").resize((W, H), Image.LANCZOS).convert("RGBA")
    alpha = layer.getchannel("A")
    bbox = alpha.getbbox()

    # Scrims: how much to darken depends on how bright the photo is where
    # the type lands.
    if st.scrim != "none" and bbox:
        luma = _region_luma(base, bbox)
        if st.scrim == "left":
            base.alpha_composite(_left_scrim(min(0.85, 0.45 + luma * 0.6)))
        else:
            base.alpha_composite(_vignette(0.55 if layout == "arc" else 0.45))
        halo = _blur_alpha(alpha, 70)
        base.alpha_composite(_tint(halo, (0, 0, 0), min(1.6, 0.55 + luma * 1.6)))
    if st.glow is not None:
        color, radius, strength = st.glow
        base.alpha_composite(_tint(_blur_alpha(alpha, radius), color, strength))
        if st.neon:
            base.alpha_composite(_tint(_blur_alpha(alpha, radius * 2.6), color, 1.3))
    if st.hard_shadow is not None:
        off, color = st.hard_shadow
        o = max(3, int(round(off * (st.ref_size or 150.0) / 100)))
        base.alpha_composite(_tint(alpha, color, 1.0, offset=(o, o)))
    else:
        base.alpha_composite(_tint(_blur_alpha(alpha, 9), (0, 0, 0), 0.75, offset=(0, 6)))
    base.alpha_composite(layer)
    return base.convert("RGB").resize(OUT_SIZE, Image.LANCZOS)


def background_ass(look: L.Look, width: int, height: int) -> str:
    """ASS document for the poster's background pass: the surfaces the look
    paints itself (flat card, gradient) and nothing else — no text and no
    letterbox (a thumbnail has no room to waste on cinema bars)."""
    bare = dataclasses.replace(look, letterbox=False)
    doc = L.build_look_ass([], bare, width=width, height=height, duration=1.0)
    if look.background == "flat" and look.flat_colors:
        doc += L._dialogue(0, 0.0, 1.0, L._rect(width, height, 0, 0, width, height,
                                                look.flat_colors[0])) + "\n"
    return doc
