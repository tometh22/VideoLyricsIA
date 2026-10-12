"""Lyric look variants: genre-aware, operator-overridable, at no AI cost.

A look (lyric_looks.LOOKS) fixes a MOTION + COMPOSITION taken from the
reference lyric videos. A variant keeps that motion and swaps the rest —
font pair, palette, grade and overlay — from curated pools for the song's
genre family, so two metal songs with the same look never come out alike.
Everything still renders in the single libass pass: a variant costs render
seconds, never a provider call.

Resolution order (`resolve`):
  1. base look (lyric_looks.LOOKS[look_id])
  2. variant for (genre family, job seed, variant number):
     - variant 0 in the look's NATIVE family (or with no genre) = the look as
       designed; in "heavy" the curated skin (lyric_looks.SKINS) when there is
       one; otherwise a recipe drawn from the family pools.
  3. operator overrides (fonts, three colours) on top — the motion never
     changes.

Determinism: the same (job, look, genre, variant, overrides) always yields the
same Look, so the editor preview, the master, the short, retries and edits all
match. `choose_auto_variant` skips recipe signatures already used by sibling
songs (same campaign + look) so a catalogue doesn't repeat itself.
"""
from __future__ import annotations

import colorsys
import dataclasses
import hashlib
import os
import random
import re

import lyric_looks as L

# --- Genres & families ------------------------------------------------------------

GENRES = {
    "metal": ("heavy", "Metal"), "rock": ("heavy", "Rock"), "punk": ("heavy", "Punk"),
    "pop": ("pop", "Pop"), "latin": ("pop", "Latino"), "reggaeton": ("pop", "Reggaetón"),
    "hiphop": ("urban", "Hip hop / trap"), "electronic": ("urban", "Electrónica"),
    "ballad": ("soft", "Balada"), "folk": ("soft", "Folk / folclore"), "indie": ("soft", "Indie"),
}
FAMILIES = {"heavy": "Rock / metal", "pop": "Pop / latino",
            "urban": "Urbano / electrónica", "soft": "Balada / folk / indie"}


def normalize_genre(genre) -> str:
    g = str(genre or "").strip().lower()
    return g if g in GENRES else ""


def family_of(genre) -> str:
    g = normalize_genre(genre)
    return GENRES[g][0] if g else ""


# The family each look was designed for: variant 0 there is the original.
NATIVE = {
    "cosmico": "urban", "cine": "soft", "pincel": "heavy", "pop70": "pop",
    "pelicula": "soft", "cinetico": "pop", "neon": "urban", "chat": "pop",
    "cuaderno": "pop", "bloque": "urban", "arco": "urban", "perspectiva": "urban",
    "duotono": "pop", "romantico": "soft", "degrade": "pop", "y2k": "pop",
}

# Looks shown first in the wizard for each family.
RECOMMENDED = {
    "heavy": ("cinetico", "pincel", "bloque", "neon", "cine", "duotono", "perspectiva", "cosmico"),
    "pop": ("cinetico", "pop70", "chat", "cuaderno", "y2k", "degrade", "duotono", "neon"),
    "urban": ("bloque", "neon", "cinetico", "y2k", "perspectiva", "cosmico", "arco", "duotono"),
    "soft": ("cine", "romantico", "pelicula", "cuaderno", "arco", "degrade", "cosmico", "chat"),
}

# --- Font catalogue ------------------------------------------------------------------
# id → (file, label, css family, css weight, families, roles)
# roles: "display" (impact / key word), "text" (lyric rows), "hand" (handwriting),
#        "script" (connected script), "ui" (plain readable sans for chat bubbles).
FONTS = {
    "anton": ("Anton-Regular.ttf", "Anton", "Anton", 400, ("heavy", "urban"), ("text", "display")),
    "audiowide": ("Audiowide-Regular.ttf", "Audiowide", "Audiowide", 400, ("urban", "pop"), ("text", "display")),
    "bebas-neue": ("BebasNeue-Regular.ttf", "Bebas Neue", "Bebas Neue", 400, ("heavy", "urban"), ("text",)),
    "big-shoulders": ("BigShouldersDisplay-Black.ttf", "Big Shoulders", "Big Shoulders Display", 900, ("heavy", "urban", "pop"), ("text", "display")),
    "big-shoulders-light": ("BigShouldersDisplay-Light.ttf", "Big Shoulders Light", "Big Shoulders Display", 300, ("urban", "heavy"), ("text",)),
    "black-ops-one": ("BlackOpsOne-Regular.ttf", "Black Ops One", "Black Ops One", 400, ("heavy", "urban"), ("text", "display")),
    "bungee": ("Bungee-Regular.ttf", "Bungee", "Bungee", 400, ("heavy", "pop", "urban"), ("text", "display")),
    "caveat": ("Caveat-Bold.ttf", "Caveat", "Caveat", 700, ("pop", "soft"), ("hand", "script", "text")),
    "creepster": ("Creepster-Regular.ttf", "Creepster", "Creepster", 400, ("heavy",), ("display",)),
    "fredoka": ("Fredoka-SemiBold.ttf", "Fredoka", "Fredoka", 600, ("pop",), ("text", "ui")),
    "jost": ("Jost-Bold.ttf", "Jost", "Jost", 700, ("soft", "urban"), ("text", "ui")),
    "knewave": ("Knewave-Regular.ttf", "Knewave", "Knewave", 400, ("heavy", "pop"), ("text", "display", "hand")),
    "marcellus": ("Marcellus-Regular.ttf", "Marcellus", "Marcellus", 400, ("soft",), ("text", "display")),
    "metal-mania": ("MetalMania-Regular.ttf", "Metal Mania", "Metal Mania", 400, ("heavy",), ("display",)),
    "michroma": ("Michroma-Regular.ttf", "Michroma", "Michroma", 400, ("urban", "pop"), ("text", "display")),
    "montserrat": ("Montserrat-ExtraBold.ttf", "Montserrat", "Montserrat", 800, ("pop", "urban", "soft"), ("text", "ui", "display")),
    "neonderthaw": ("Neonderthaw-Regular.ttf", "Neonderthaw", "Neonderthaw", 400, ("urban", "soft"), ("script",)),
    "new-rocker": ("NewRocker-Regular.ttf", "New Rocker", "New Rocker", 400, ("heavy",), ("display",)),
    "nunito": ("Nunito-ExtraBold.ttf", "Nunito", "Nunito", 800, ("pop", "soft"), ("text", "ui")),
    "oswald": ("Oswald-Bold.ttf", "Oswald", "Oswald", 700, ("heavy", "urban", "soft"), ("text",)),
    "outfit": ("Outfit-Bold.ttf", "Outfit", "Outfit", 700, ("pop", "soft"), ("text", "ui")),
    "permanent-marker": ("PermanentMarker-Regular.ttf", "Permanent Marker", "Permanent Marker", 400, ("pop", "urban"), ("hand", "text", "display")),
    "pirata-one": ("PirataOne-Regular.ttf", "Pirata One", "Pirata One", 400, ("heavy",), ("display",)),
    "poppins": ("Poppins-Bold.ttf", "Poppins", "Poppins", 700, ("pop",), ("text", "ui")),
    "quicksand": ("Quicksand-Bold.ttf", "Quicksand", "Quicksand", 700, ("soft",), ("text", "ui")),
    "roboto": ("Roboto-Bold.ttf", "Roboto", "Roboto", 700, ("urban", "pop"), ("ui", "text")),
    "rubik-dirt": ("RubikDirt-Regular.ttf", "Rubik Dirt", "Rubik Dirt", 400, ("heavy",), ("text", "display")),
    "rubik-glitch": ("RubikGlitch-Regular.ttf", "Rubik Glitch", "Rubik Glitch", 400, ("heavy", "urban"), ("display",)),
    "rubik-wet-paint": ("RubikWetPaint-Regular.ttf", "Rubik Wet Paint", "Rubik Wet Paint", 400, ("heavy",), ("text", "display")),
    "sacramento": ("Sacramento-Regular.ttf", "Sacramento", "Sacramento", 400, ("soft",), ("script",)),
    "shrikhand": ("Shrikhand-Regular.ttf", "Shrikhand", "Shrikhand", 400, ("pop",), ("text", "display")),
    "tilt-neon": ("TiltNeon-Regular.ttf", "Tilt Neon", "Tilt Neon", 400, ("urban", "pop"), ("text",)),
    "unifraktur-cook": ("UnifrakturCook-Bold.ttf", "UnifrakturCook", "UnifrakturCook", 700, ("heavy",), ("display",)),
}
_FONT_BY_FILE = {v[0]: k for k, v in FONTS.items()}

# What each look's lyric font must be able to do.
LOOK_TEXT_ROLE = {"cuaderno": "hand", "romantico": "script", "chat": "ui"}

# Looks that paint their own surfaces (cards, bubbles, gradient): recipes swap
# only their fonts; operator colours still apply.
_FONT_ONLY_RECIPES = {"pop70", "degrade", "chat"}

# --- Palettes (ink, alt, accent) & grades & overlays per family ---------------------

PALETTES = {
    "heavy": {"sangre": ("#F2EEE6", "#CFC9BE", "#D0101E"), "fuego": ("#FFF1D6", "#FFB000", "#FF3D00"),
              "acero": ("#EAF2F6", "#9FB3BE", "#4FC3F7"), "toxico": ("#EEF6E0", "#A9B89A", "#9EF01A"),
              "hueso": ("#EFE6D2", "#B8A88A", "#C8A24A"), "mono": ("#FFFFFF", "#BDBDBD", "#E10600")},
    "pop": {"chicle": ("#FFFFFF", "#FFD1E8", "#FF4FA3"), "limon": ("#FFFFFF", "#FFF3B0", "#FFD23F"),
            "cielo": ("#FFFFFF", "#CDE7FF", "#3FA9FF"), "coral": ("#FFF6EE", "#FFD3C2", "#FF6B5B"),
            "menta": ("#FFFFFF", "#D7FFF1", "#2EE6A6"), "violeta": ("#FFFFFF", "#E9DBFF", "#9B5CFF")},
    "urban": {"acido": ("#FFFFFF", "#BDBDBD", "#C6FF00"), "cian": ("#FFFFFF", "#9FE8FF", "#00E5FF"),
              "oro": ("#FFFFFF", "#E8D9A8", "#FFC300"), "magenta": ("#FFFFFF", "#F5B8E6", "#FF2BD6"),
              "blanco_rojo": ("#FFFFFF", "#CCCCCC", "#FF2A2A"), "hielo": ("#EAF6FF", "#9FC6E0", "#5ED8FF")},
    "soft": {"crema": ("#FFF8EE", "#E8DCC8", "#E2B86A"), "rosa": ("#FFF5F5", "#F2D3D6", "#E8899A"),
             "salvia": ("#F5F7F0", "#CFD8C6", "#8DB38B"), "dorado": ("#FFFFFF", "#EADFC8", "#D4A853"),
             "lavanda": ("#F8F5FF", "#DCD3EE", "#A88BD9"), "azul_noche": ("#F2F5FA", "#C9D3E6", "#7FA3D9")},
}

# grade id → (ffmpeg chain, tint hue in degrees or None)
GRADES = {
    "heavy": {
        "oscuro": ("eq=contrast=1.28:saturation=0.55:brightness=-0.10,vignette=angle=PI/3.6", None),
        "bleach": ("eq=contrast=1.42:saturation=0.25:brightness=-0.04,vignette=angle=PI/4", None),
        "rojizo": ("eq=contrast=1.25:saturation=0.6:brightness=-0.08,colorbalance=rs=0.18:gs=-0.05:bs=-0.08,vignette=angle=PI/3.8", 0),
        "frio": ("eq=contrast=1.22:saturation=0.5:brightness=-0.08,colorbalance=rs=-0.10:bs=0.14,vignette=angle=PI/3.8", 210),
        "sepia": ("eq=contrast=1.2:saturation=0.35:brightness=-0.06,colorbalance=rs=0.12:gs=0.05:bs=-0.12,vignette=angle=PI/3.6", 35),
    },
    "pop": {
        "brillante": ("eq=saturation=1.18:contrast=1.06", None),
        "pastel": ("eq=saturation=0.85:brightness=0.03:contrast=0.96", None),
        "calido": ("eq=saturation=1.08:contrast=1.04,colorbalance=rs=0.08:bs=-0.06", 30),
        "fresco": ("eq=saturation=1.1:contrast=1.04,colorbalance=rs=-0.05:bs=0.08", 200),
    },
    "urban": {
        "nocturno": ("eq=brightness=-0.12:contrast=1.15:saturation=0.9,vignette=angle=PI/4", None),
        "contraste": ("eq=contrast=1.3:saturation=0.75:brightness=-0.06", None),
        "frio": ("eq=contrast=1.15:saturation=0.8:brightness=-0.08,colorbalance=rs=-0.08:bs=0.12", 210),
        "verdoso": ("eq=contrast=1.12:saturation=0.8:brightness=-0.08,colorbalance=rs=-0.06:gs=0.08:bs=-0.04", 120),
    },
    "soft": {
        "calido": ("eq=contrast=0.96:saturation=0.9:brightness=0.02,colorbalance=rs=0.06:bs=-0.05,vignette=angle=PI/5", 30),
        "lavado": ("eq=contrast=0.92:saturation=0.75:brightness=0.03,curves=all='0/0.06 1/0.97',vignette=angle=PI/5", None),
        "dorado": ("eq=contrast=1.0:saturation=0.95,colorbalance=rs=0.1:gs=0.04:bs=-0.1", 40),
        "neutral": ("eq=contrast=1.02:saturation=0.9,vignette=angle=PI/5", None),
    },
}

EFFECTS = {
    "heavy": ("film", "dust", "embers", "scanlines", "rgb_glitch", ""),
    "pop": ("", "bokeh", "light", "prism", "shapes"),
    "urban": ("", "scanlines", "rgb_glitch", "light", "film", "chromatic_pulse"),
    "soft": ("", "film", "dust", "bokeh", "light", "fog"),
}


def _hue(hex_color: str) -> float | None:
    r, g, b = (int(hex_color[i:i + 2], 16) / 255.0 for i in (1, 3, 5))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    return None if s < 0.25 or l > 0.9 else h * 360.0


def _clashes(accent: str, tint) -> bool:
    """Accent the same hue as the grade's tint disappears into the scene."""
    if tint is None:
        return False
    h = _hue(accent)
    if h is None:
        return False
    d = abs(h - tint) % 360
    return min(d, 360 - d) < 35


def luminance(hex_color: str) -> float:
    r, g, b = (int(hex_color[i:i + 2], 16) / 255.0 for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


# --- Recipe -------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Recipe:
    family: str
    look: str
    variant: int
    fonts: tuple[str, str]           # font ids (main, key)
    palette: str
    colors: tuple[str, str, str]
    grade: str
    effect: str
    extra: tuple = ()
    original: bool = False           # variant 0 in the native family
    curated: bool = False            # variant 0 = hand-made skin (lyric_looks.SKINS)

    @property
    def signature(self) -> tuple:
        """What makes two songs look 'the same' to a viewer."""
        return (self.look, self.fonts, self.palette)

    def describe(self) -> str:
        if self.original:
            return "Diseño original"
        if self.curated:
            a, b = (FONTS[f][1] for f in self.fonts)
            return f"Diseño curado · {a}" + (f" + {b}" if a != b else "")
        a, b = (FONTS[f][1] for f in self.fonts)
        fonts = a if a == b else f"{a} + {b}"
        return f"{fonts} · {self.palette.replace('_', ' ')} · {self.grade}"


def seed_key_for(artist: str, song_title: str) -> str:
    """The seed both the preview and the render use: the song itself (normalised
    artist + title), so the wizard — which may not have a job id yet — shows
    exactly what the worker will render."""
    norm = lambda v: re.sub(r"\s+", " ", str(v or "").strip().lower())
    return f"{norm(artist)}|{norm(song_title)}"


def job_seed(seed_key: str) -> int:
    return int(hashlib.sha1(str(seed_key or "").encode("utf-8")).hexdigest()[:10], 16)


def _font_pool(family: str, role: str) -> list[str]:
    out = [k for k, v in FONTS.items() if family in v[4] and role in v[5]]
    return sorted(out)


def recipe_for(look_id: str, genre: str, seed_key: str, variant: int) -> Recipe:
    """Deterministic recipe for a song. Never raises for a known look."""
    look = L.LOOKS[look_id]
    family = family_of(genre) or NATIVE.get(look_id, "pop")
    variant = max(0, int(variant or 0))
    if variant == 0 and family == NATIVE.get(look_id):
        main = _FONT_BY_FILE.get(look.font_file, "montserrat")
        key = _FONT_BY_FILE.get(look.key_font or look.font_file, main)
        return Recipe(family, look_id, 0, (main, key), "original",
                      (look.primary, look.primary, look.accent or look.primary),
                      "original", look.effect, (), original=True)
    if variant == 0 and (look_id, family) in L.SKINS:
        skinned = dataclasses.replace(look, **L.SKINS[(look_id, family)])
        main = _FONT_BY_FILE.get(skinned.font_file, "montserrat")
        key = _FONT_BY_FILE.get(skinned.key_font or skinned.font_file, main)
        cols = (tuple(skinned.line_colors[:3]) if len(skinned.line_colors) >= 3
                else (skinned.primary, skinned.primary, skinned.accent or skinned.primary))
        return Recipe(family, look_id, 0, (main, key), "curada", cols, "curado",
                      skinned.effect, (), curated=True)
    text_role = LOOK_TEXT_ROLE.get(look_id, "text")
    # A look that needs a specific kind of face (handwriting, script, plain
    # UI) takes it from any family when the genre's family has none.
    mains = (_font_pool(family, text_role)
             or sorted(k for k, v in FONTS.items() if text_role in v[5])
             or _font_pool(family, "text"))
    keys = _font_pool(family, "display") or mains
    pals = PALETTES[family]
    grades = GRADES[family]
    base_seed = job_seed(seed_key)
    for attempt in range(80):
        rng = random.Random(f"{family}:{look_id}:{base_seed}:{variant}:{attempt}")
        main = rng.choice(mains)
        key = rng.choice(keys) if text_role == "text" else main
        palette = rng.choice(sorted(pals))
        grade = rng.choice(sorted(grades))
        if _clashes(pals[palette][2], grades[grade][1]):
            continue
        effect = rng.choice(EFFECTS[family])
        extra = (rng.random(), rng.random(), rng.random())
        return Recipe(family, look_id, variant, (main, key), palette, pals[palette],
                      grade, effect, extra)
    return Recipe(family, look_id, variant, (mains[0], keys[0]), sorted(pals)[0],
                  pals[sorted(pals)[0]], sorted(grades)[0], "", (0.5, 0.5, 0.5))


def choose_auto_variant(look_id: str, genre: str, seed_key: str,
                        used_signatures: set | None = None, limit: int = 40) -> int:
    """Smallest variant whose look isn't already used by a sibling song."""
    used = used_signatures or set()
    # Start each song at its own offset so a batch resolved before any sibling
    # has rendered (a campaign, a wizard batch) still spreads across variants
    # instead of all landing on variant 0.
    start = job_seed(seed_key) % 6
    for i in range(limit):
        v = (start + i) % limit
        if recipe_for(look_id, genre, seed_key, v).signature not in used:
            return v
    return start


# --- Applying fonts / colours to a look ---------------------------------------------

def _font_file(font_id: str) -> str:
    return FONTS[font_id][0]


def apply_fonts(look: L.Look, base: L.Look, main_id: str, key_id: str) -> L.Look:
    """Swap the faces, keeping the size the look was tuned for (cap height)."""
    main, key = _font_file(main_id), _font_file(key_id)
    fdir = L._FONTS_DIR
    # Relative to the face the look currently has (a recipe may already have
    # swapped and compensated it): compensating from the base again would
    # apply the correction twice.
    tuned = L.cap_per_fs(os.path.join(fdir, look.font_file))
    actual = L.cap_per_fs(os.path.join(fdir, main))
    upd = {"font_file": main,
           "font_scale": round(look.font_scale * tuned / actual, 3)}
    if look.layout == "kinetic":
        upd.update(extra_fonts=(main,), key_font=key)
    elif look.layout == "block":
        upd["extra_fonts"] = (key,)
    elif look.motion == "neon":
        upd["extra_fonts"] = (key,)
    elif look.layout in ("keyword",) or look.motion == "zoom_through":
        upd["key_font"] = key
    else:
        upd["key_font"] = key
    return dataclasses.replace(look, **upd)


def apply_colors(look: L.Look, ink: str, alt: str, accent: str,
                 *, explicit: bool = False) -> L.Look:
    """Paint the look with (ink, alt, accent) in the way each look uses colour."""
    upd = {"primary": ink, "accent": accent}
    if look.layout == "kinetic":
        upd["line_colors"] = (ink, alt, accent)
        if explicit or look.background != "flat":
            upd["card_palettes"] = ()
    elif look.motion == "neon":
        upd.update(primary="#FFFFFF" if luminance(ink) < 0.6 else ink,
                   line_colors=(accent, alt, accent, ink))
    elif look.motion == "boil":
        # The duotone poster is painted from the accent: lettering never uses it.
        upd["line_colors"] = (ink, alt, ink, "#FFFFFF")
    elif look.layout == "chat":
        incoming = look.flat_colors[0] if look.flat_colors else "#E9E9EB"
        upd.update(primary=ink if explicit else look.primary,
                   flat_colors=(incoming, accent), accent="#FFFFFF")
    if look.flat_accents and explicit:
        upd["flat_accents"] = ()
    return dataclasses.replace(look, **upd)


def _duotone_grade(ink: str, accent: str) -> str:
    dr, dg, db = (int(accent[i:i + 2], 16) / 255 * 0.35 for i in (1, 3, 5))
    lr, lg, lb = (int(ink[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return (f"hue=s=0,eq=contrast=1.35,curves=r='0/{dr:.2f} 1/{lr:.2f}'"
            f":g='0/{dg:.2f} 1/{lg:.2f}':b='0/{db:.2f} 1/{lb:.2f}'")


def _second_face(look: L.Look) -> str:
    """The look's second typeface: the key-word face for kinetic/keyword
    looks, the alternate face kept in extra_fonts for block and neon."""
    if look.layout in ("block",) or look.motion == "neon":
        return look.extra_fonts[0] if look.extra_fonts else look.font_file
    return look.key_font or look.font_file


def build_from_recipe(rec: Recipe, *, bg_luma: float | None = None) -> L.Look:
    base = L.LOOKS[rec.look]
    if rec.original:
        return base
    if rec.curated:
        return dataclasses.replace(base, **L.SKINS[(rec.look, rec.family)])
    look = apply_fonts(base, base, *rec.fonts)
    if rec.look not in _FONT_ONLY_RECIPES:
        look = apply_colors(look, *rec.colors)
        grade = GRADES[rec.family][rec.grade][0]
        if bg_luma is not None and bg_luma > 0.5:
            grade = "eq=brightness=-0.10," + grade
        upd = dict(grade=grade, effect=rec.effect, outline=max(look.outline, 2.5),
                   outline_color="#000000", shadow=max(look.shadow, 4),
                   shadow_color="#000000", shadow_alpha=min(look.shadow_alpha, 0x40))
        r1, r2, _r3 = rec.extra or (0.5, 0.5, 0.5)
        if look.layout == "kinetic":
            upd.update(background="source", flat_colors=(), card_palettes=(),
                       connector_tilt=rec.family != "heavy",
                       kinetic_decos=(("slash", "burst") if rec.family == "heavy"
                                      else ("burst", "bubble", "swoosh")),
                       beat_shake=rec.family in ("heavy", "urban"),
                       cascade=r2 < 0.5)
        if look.motion == "boil":
            upd["grade"] = _duotone_grade(rec.colors[0], rec.colors[2])
            upd.update(outline_color="#000000", shadow_alpha=0)
        if look.motion == "neon":
            upd.update(outline=0, shadow=0,
                       grade="eq=brightness=-0.24:saturation=0.5:contrast=1.15,vignette=angle=PI/3.6")
        if look.motion == "zoom_through":
            upd.update(primary=rec.colors[2] if rec.family == "heavy" else look.primary,
                       outline=4)
        if look.motion == "neon" or look.layout in ("chat",):
            upd.pop("outline_color", None)
        if rec.family == "heavy" and look.layout == "floor":
            upd["font_scale"] = look.font_scale * 1.3
        if look.layout == "keyword":
            upd["lead_scale"] = max(look.lead_scale, 0.62)
        look = dataclasses.replace(look, **upd)
    return look


# --- Full resolution (recipe + operator overrides) ------------------------------------

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def _hex_or_empty(v) -> str:
    v = str(v or "").strip()
    return v.upper() if _HEX.match(v) else ""


def normalize_overrides(*, font_main="", font_key="", ink="", alt="", accent="") -> dict:
    """Clean operator overrides: unknown fonts and malformed colours are
    dropped; the wizard's default white text means 'not chosen'."""
    ink = _hex_or_empty(ink)
    if ink == "#FFFFFF":
        ink = ""
    return {
        "font_main": font_main if font_main in FONTS else "",
        "font_key": font_key if font_key in FONTS else "",
        "ink": ink, "alt": _hex_or_empty(alt), "accent": _hex_or_empty(accent),
    }


def resolve(look_id: str, *, genre: str = "", variant=0, seed_key: str = "",
            overrides: dict | None = None, bg_luma: float | None = None):
    """(Look, info) for a song. `info` mirrors /lyric-looks/resolve."""
    base = L.get_look(look_id)
    if base is None:
        return None, {}
    ov = normalize_overrides(**(overrides or {}))
    try:
        variant = max(0, int(variant or 0))
    except (TypeError, ValueError):
        variant = 0
    rec = recipe_for(base.id, genre, seed_key, variant)
    look = build_from_recipe(rec, bg_luma=bg_luma)
    main_id = _FONT_BY_FILE.get(look.font_file, rec.fonts[0])
    key_id = _FONT_BY_FILE.get(_second_face(look), rec.fonts[1])
    if ov["font_main"] or ov["font_key"]:
        look = apply_fonts(look, base, ov["font_main"] or main_id, ov["font_key"] or key_id)
        main_id, key_id = ov["font_main"] or main_id, ov["font_key"] or key_id
    cur = effective_colors(look, rec)
    if ov["ink"] or ov["alt"] or ov["accent"]:
        ink, alt, accent = (ov["ink"] or cur[0], ov["alt"] or cur[1], ov["accent"] or cur[2])
        look = apply_colors(look, ink, alt, accent, explicit=True)
        if look.motion == "boil":
            look = dataclasses.replace(look, grade=_duotone_grade(ink, accent))
        cur = (ink, alt, accent)
    warnings = []
    if luminance(cur[0]) < 0.35 and look.layout != "chat":
        warnings.append("low_contrast_ink")
    if luminance(cur[2]) < 0.12:
        warnings.append("low_contrast_accent")
    if cur[2].upper() == cur[0].upper():
        warnings.append("accent_same_as_text")
    font_info = lambda fid: {"id": fid, "label": FONTS[fid][1], "css_family": FONTS[fid][2],
                             "weight": FONTS[fid][3]}
    info = {
        "look": base.id, "genre": normalize_genre(genre), "family": rec.family,
        "variant": rec.variant, "description": rec.describe(),
        "fonts": {"main": font_info(main_id), "key": font_info(key_id)},
        "colors": {"ink": cur[0], "alt": cur[1], "accent": cur[2]},
        "overridden": {"font_main": bool(ov["font_main"]), "font_key": bool(ov["font_key"]),
                       "ink": bool(ov["ink"]), "alt": bool(ov["alt"]), "accent": bool(ov["accent"])},
        "background": look.background, "signature": list(rec.signature[1]) + [rec.signature[2]],
        # How the look is drawn in this variant (for an exact preview).
        "style": {
            "decorations": list(look.kinetic_decos) if look.layout == "kinetic" else [],
            # Kinetic connectors are set in the look's second face
            # (extra_fonts[0]: Caveat script in pop, the main face in heavy).
            "connector_font": (_FONT_BY_FILE.get(look.extra_fonts[0], main_id)
                               if look.extra_fonts else main_id)
            if look.layout == "kinetic" else None,
            "connector_color": (cur[1] if not look.connector_tilt else cur[2])
            if look.layout == "kinetic" else None,
            "connector_tilt": bool(look.connector_tilt),
            "beat_shake": bool(look.beat_shake),
            "cascade": bool(look.cascade),
            "letterbox": bool(look.letterbox),
            "effect": look.effect or "",
        },
        "warnings": warnings,
    }
    return look, info


def effective_colors(look: L.Look, rec: Recipe) -> tuple[str, str, str]:
    if look.line_colors and look.layout == "kinetic":
        return tuple(look.line_colors[:3])  # type: ignore[return-value]
    if look.card_palettes:
        return tuple(look.card_palettes[0][:3])  # type: ignore[return-value]
    if not rec.original:
        return rec.colors
    return (look.primary, look.line_colors[1] if len(look.line_colors) > 1 else look.primary,
            look.accent or (look.line_colors[0] if look.line_colors else look.primary))


def catalog() -> dict:
    return {
        "families": [{"id": k, "label": v} for k, v in FAMILIES.items()],
        "genres": [{"id": g, "family": f, "label": lbl} for g, (f, lbl) in GENRES.items()],
        "fonts": [{"id": k, "label": v[1], "css_family": v[2], "weight": v[3],
                   "families": list(v[4]), "roles": list(v[5])}
                  for k, v in sorted(FONTS.items(), key=lambda kv: kv[1][1].lower())],
        "recommended": {k: list(v) for k, v in RECOMMENDED.items()},
        "native": dict(NATIVE),
    }
