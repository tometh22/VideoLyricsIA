"""Lyric look recipes: hundreds of variants per genre family at no AI cost.

A look (lyric_looks.LOOKS) fixes a motion + composition taken from the
reference lyric videos (kinetic build, word slam, justified block, fly-through,
stamped ink, neon sign, cinema key word, floor perspective). A recipe keeps
that motion and swaps every other ingredient — font pair, palette, decorations,
grade and overlay — from curated pools for the song's genre family. Everything
still renders in the single libass pass, so a variant costs render seconds and
no provider call.

Determinism: the same (job, seed) always yields the same variant, so retries
and re-renders look identical. `avoid` lets a batch/campaign skip combinations
already used by sibling songs, so songs of the same artist/genre don't repeat.

Readability guardrails: every heavy palette has a light ink, every variant
keeps a dark outline + shadow, and a bright background gets an extra darkening
grade (bg_luma from the job's real background).
"""
from __future__ import annotations

import colorsys
import dataclasses
import os
import random

import lyric_looks as L

# --- Ingredient pools ---------------------------------------------------------

# Bases: the motion/composition families we analysed in the reference videos.
BASES = {
    "heavy": ("cinetico", "pincel", "bloque", "cosmico", "duotono", "neon",
              "cine", "perspectiva"),
}

# (main face, display/key face). All OFL/Apache, unmodified, with Ñ/tildes.
FONT_PAIRS = {
    "heavy": (
        ("RubikDirt-Regular.ttf", "MetalMania-Regular.ttf"),
        ("Anton-Regular.ttf", "UnifrakturCook-Bold.ttf"),
        ("BebasNeue-Regular.ttf", "PirataOne-Regular.ttf"),
        ("BlackOpsOne-Regular.ttf", "NewRocker-Regular.ttf"),
        ("BigShouldersDisplay-Black.ttf", "Creepster-Regular.ttf"),
        ("Oswald-Bold.ttf", "MetalMania-Regular.ttf"),
        ("RubikWetPaint-Regular.ttf", "BlackOpsOne-Regular.ttf"),
        ("Bungee-Regular.ttf", "RubikGlitch-Regular.ttf"),
    ),
}

# (ink, alt, accent)
PALETTES = {
    "heavy": {
        "sangre": ("#F2EEE6", "#CFC9BE", "#D0101E"),
        "fuego": ("#FFF1D6", "#FFB000", "#FF3D00"),
        "acero": ("#EAF2F6", "#9FB3BE", "#4FC3F7"),
        "toxico": ("#EEF6E0", "#A9B89A", "#9EF01A"),
        "hueso": ("#EFE6D2", "#B8A88A", "#C8A24A"),
        # Black & white with one red — a palette with no accent left the
        # key word indistinguishable (La Renga, perspectiva).
        "mono": ("#FFFFFF", "#BDBDBD", "#E10600"),
    },
}

GRADES = {
    "heavy": {
        "oscuro": "eq=contrast=1.28:saturation=0.55:brightness=-0.10,vignette=angle=PI/3.6",
        "bleach": "eq=contrast=1.42:saturation=0.25:brightness=-0.04,vignette=angle=PI/4",
        "rojizo": ("eq=contrast=1.25:saturation=0.6:brightness=-0.08,"
                   "colorbalance=rs=0.18:gs=-0.05:bs=-0.08,vignette=angle=PI/3.8"),
        "frio": ("eq=contrast=1.22:saturation=0.5:brightness=-0.08,"
                 "colorbalance=rs=-0.10:bs=0.14,vignette=angle=PI/3.8"),
        "sepia": ("eq=contrast=1.2:saturation=0.35:brightness=-0.06,"
                  "colorbalance=rs=0.12:gs=0.05:bs=-0.12,vignette=angle=PI/3.6"),
    },
}

EFFECTS = {"heavy": ("film", "dust", "embers", "scanlines", "rgb_glitch", "")}

# Grades that clash with a palette (a cold grade under a fire palette).
_CLASH = {("fuego", "frio"), ("toxico", "rojizo"), ("acero", "rojizo"),
          ("acero", "sepia"), ("hueso", "frio"),
          # Red accent over a red-tinted scene disappears (Bersuit, cósmico).
          ("mono", "rojizo"), ("sangre", "rojizo"), ("fuego", "rojizo")}


# --- Palette from the background ------------------------------------------------

def background_palette(dominant_rgb: tuple[int, int, int]) -> tuple[str, str, str]:
    """A palette that belongs to THIS background: bone ink, the background's
    own hue as the quiet tone, and its complementary hue (saturated, bright)
    as the accent — so the key word pops against the scene."""
    r, g, b = (c / 255.0 for c in dominant_rgb)
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    quiet = colorsys.hls_to_rgb(h, 0.72, min(0.35, s + 0.1))
    acc = colorsys.hls_to_rgb((h + 0.5) % 1.0, 0.55, 0.85)
    hx = lambda c: "#%02X%02X%02X" % tuple(int(round(v * 255)) for v in c)
    return ("#F2EEE6", hx(quiet), hx(acc))


# --- Recipe -------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Recipe:
    family: str
    base: str
    fonts: tuple[str, str]
    palette: str
    colors: tuple[str, str, str]
    grade: str
    effect: str
    extra: tuple = ()

    @property
    def signature(self) -> tuple:
        """What makes two variants look 'the same' to a viewer."""
        return (self.base, self.fonts, self.palette)

    def describe(self) -> str:
        return (f"{self.base} · {self.fonts[0].split('-')[0]}+{self.fonts[1].split('-')[0]}"
                f" · {self.palette} · {self.grade} · {self.effect or 'sin efecto'}")


def pick_recipe(family: str, seed: int, *, avoid: set | None = None,
                bg_dominant: tuple[int, int, int] | None = None,
                bases: tuple[str, ...] | None = None) -> Recipe:
    """Deterministic recipe for `seed`, skipping signatures in `avoid`."""
    avoid = avoid or set()
    pals = dict(PALETTES[family])
    if bg_dominant is not None:
        pals["del_fondo"] = background_palette(bg_dominant)
    for attempt in range(64):
        rng = random.Random(f"{family}:{seed}:{attempt}")
        base = rng.choice(bases or BASES[family])
        fonts = rng.choice(FONT_PAIRS[family])
        palette = rng.choice(sorted(pals))
        grade = rng.choice(sorted(GRADES[family]))
        if (palette, grade) in _CLASH:
            continue
        effect = rng.choice(EFFECTS[family])
        extra = (rng.random(), rng.random(), rng.random())
        r = Recipe(family, base, fonts, palette, pals[palette], grade, effect, extra)
        if r.signature not in avoid:
            return r
    return r


def build_variant(recipe: Recipe, *, bg_luma: float | None = None) -> L.Look:
    """The base look with every ingredient of the recipe applied."""
    base = L.LOOKS[recipe.base]
    main, key = recipe.fonts
    ink, alt, accent = recipe.colors
    grade = GRADES[recipe.family][recipe.grade]
    if bg_luma is not None and bg_luma > 0.5:
        # Bright scene: darken further so light ink keeps its contrast.
        grade = "eq=brightness=-0.10," + grade
    r1, r2, r3 = recipe.extra
    common = dict(
        primary=ink, accent=accent, outline=3, outline_color="#000000",
        shadow=5, shadow_color="#000000", shadow_alpha=0x30,
        grade=grade, effect=recipe.effect, background="source",
        flat_colors=(), card_palettes=(), force_case="",
    )
    b = recipe.base
    if b == "cinetico":
        over = dict(font_file=main, extra_fonts=(main,), key_font=key,
                    line_colors=(ink, alt, accent), connector_tilt=False,
                    kinetic_decos=("slash", "burst") if r1 < 0.6 else ("burst",),
                    beat_shake=True, cascade=r2 < 0.5)
    elif b == "pincel":
        over = dict(font_file=main, tilt=round(2 + r1 * 5, 1), key_scale=1.4 + r2 * 0.3,
                    row_width=0.55 + r3 * 0.15)
    elif b == "bloque":
        over = dict(font_file=main, extra_fonts=(key,), row_width=0.42 + r1 * 0.12)
    elif b == "cosmico":
        # One line flying at the camera has no key-word hierarchy: give it the
        # display face, the accent colour and more size so it still hits.
        over = dict(font_file=key, font_scale=1.45 + r2 * 0.25, primary=accent,
                    outline=4)
    elif b == "duotono":
        # The poster is painted in the accent; the lettering must never use it.
        over = dict(font_file=main, line_colors=(ink, alt, ink, "#FFFFFF"),
                    outline_color="#000000", shadow_color="#000000", shadow_alpha=0)
        # Two-ink poster in the palette's own colours.
        dr, dg, db = (int(accent[i:i + 2], 16) / 255 * 0.35 for i in (1, 3, 5))
        lr, lg, lb = (int(ink[i:i + 2], 16) / 255 for i in (1, 3, 5))
        common["grade"] = (f"hue=s=0,eq=contrast=1.35,curves=r='0/{dr:.2f} 1/{lr:.2f}'"
                           f":g='0/{dg:.2f} 1/{lg:.2f}':b='0/{db:.2f} 1/{lb:.2f}'")
    elif b == "neon":
        over = dict(font_file=main, extra_fonts=(key,), font_scale=1.5,
                    line_colors=(accent, ink, alt, accent), outline=0, shadow=0)
        common.update(outline=0, shadow=0)
        common["grade"] = "eq=brightness=-0.24:saturation=0.5:contrast=1.15,vignette=angle=PI/3.6"
    elif b == "cine":
        over = dict(font_file=key, glow=4, letterbox=r1 < 0.6,
                    key_scale=1.5 + r2 * 0.3, lead_scale=0.68)
    else:  # perspectiva
        over = dict(font_file=main, font_scale=L.LOOKS["perspectiva"].font_scale * 1.3)
    over.setdefault("font_file", main)
    # Same visual size as the look was tuned for: compensate the cap height
    # of the swapped-in face (Bungee renders ~40% smaller per \\fs than
    # Montserrat, UnifrakturCook ~30% larger).
    fdir = L._FONTS_DIR
    tuned = L.cap_per_fs(os.path.join(fdir, base.font_file))
    actual = L.cap_per_fs(os.path.join(fdir, over["font_file"]))
    over["font_scale"] = round(over.get("font_scale", base.font_scale) * tuned / actual, 3)
    look = dataclasses.replace(base, **{**common, **over})
    if b == "duotono":
        look = dataclasses.replace(look, line_colors=(ink, alt, ink, "#FFFFFF"))
    return look


def pick_batch(family: str, seeds: list[int], *,
               bg_dominants: list | None = None) -> list[Recipe]:
    """Recipes for a batch of songs (a campaign, an artist's catalogue) that
    look as different as possible: no motion repeats until every base was
    used, and no font pair / palette repeats between consecutive songs."""
    out: list[Recipe] = []
    used_sigs: set = set()
    for i, seed in enumerate(seeds):
        used_bases = {r.base for r in out[-len(BASES[family]):]}
        free = tuple(b for b in BASES[family] if b not in used_bases) or BASES[family]
        prev = out[-1] if out else None
        bg = bg_dominants[i] if bg_dominants else None
        for attempt in range(40):
            r = pick_recipe(family, seed * 100 + attempt, avoid=used_sigs,
                            bg_dominant=bg, bases=free)
            if prev is None or (r.fonts != prev.fonts and r.palette != prev.palette
                                and r.effect != prev.effect):
                break
        used_sigs.add(r.signature)
        out.append(r)
    return out
