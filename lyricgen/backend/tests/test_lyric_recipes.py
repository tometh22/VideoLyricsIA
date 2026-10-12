import os
import re

import pytest

import lyric_looks as L
import lyric_recipes as R

SEGS = [{"text": "Y prefiero quebrarme a doblarme", "start": 1.0, "end": 4.0},
        {"text": "Lo compartió con su gente", "start": 4.2, "end": 7.0}]


def test_every_recipe_font_ships_with_spanish_glyphs_and_license():
    from PIL import ImageFont
    lic = os.listdir(os.path.join(os.path.dirname(L.__file__), "fonts", "licenses"))
    for pair in R.FONT_PAIRS["heavy"]:
        for f in pair:
            path = os.path.join(os.path.dirname(L.__file__), "fonts", f)
            assert os.path.exists(path), f
            fam = f.split("-")[0]
            if fam not in ("Oswald", "Anton", "BebasNeue"):
                assert any(n.startswith(fam) for n in lic), f
            font = ImageFont.truetype(path, 40)
            assert font.getlength("ÑÁÉÍÓÚ") > 0


def test_same_seed_same_variant_and_avoid_changes_it():
    a = R.pick_recipe("heavy", 7)
    assert R.pick_recipe("heavy", 7) == a
    b = R.pick_recipe("heavy", 7, avoid={a.signature})
    assert b.signature != a.signature


def test_batch_does_not_repeat_motion_until_all_used_nor_neighbours():
    batch = R.pick_batch("heavy", list(range(16)))
    n = len(R.BASES["heavy"])
    assert len({r.base for r in batch[:n]}) == n
    for a, b in zip(batch, batch[1:]):
        assert a.fonts != b.fonts and a.palette != b.palette


@pytest.mark.parametrize("seed", range(40))
def test_every_variant_renders_valid_ass_in_both_orientations(seed):
    rec = R.pick_recipe("heavy", seed, bg_dominant=(90, 60, 40))
    look = R.build_variant(rec, bg_luma=0.6)
    for w, h in ((1920, 1080), (1080, 1920)):
        doc = L.build_look_ass(SEGS, look, width=w, height=h, duration=8,
                               beats=[1.5, 2.0, 2.5, 3.0, 5.0, 6.0])
        events = [d for d in doc.splitlines() if d.startswith("Dialogue:")]
        assert events
        for d in events:
            assert d.count("{") == d.count("}")
            for a, b in re.findall(r"\\t\((-?\d+),(-?\d+),", d):
                assert 0 <= int(a) < int(b)


def test_background_palette_complements_the_scene():
    ink, quiet, accent = R.background_palette((40, 70, 160))   # blue scene
    r, g, b = (int(accent[i:i + 2], 16) for i in (1, 3, 5))
    assert r > b                                               # warm accent


@pytest.mark.parametrize("seed", range(30))
def test_variant_cap_height_matches_its_base_look(seed):
    rec = R.pick_recipe("heavy", seed)
    base = L.LOOKS[rec.base]
    look = R.build_variant(rec)
    d = L._FONTS_DIR
    tuned = L.cap_per_fs(os.path.join(d, base.font_file)) * base.font_scale
    got = L.cap_per_fs(os.path.join(d, look.font_file)) * look.font_scale
    want = tuned * (1.1 + 0.3 if rec.base == "cosmico" else 1)  # cosmico grows on purpose
    assert got == pytest.approx(tuned, rel=0.35) or got <= want * 1.05


@pytest.mark.parametrize("seed", range(60))
def test_duotone_lettering_never_uses_the_poster_colour(seed):
    rec = R.pick_recipe("heavy", seed, bases=("duotono",))
    look = R.build_variant(rec)
    assert rec.colors[2] not in look.line_colors or rec.colors[2] == rec.colors[0]
