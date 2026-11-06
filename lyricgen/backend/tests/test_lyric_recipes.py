import os
import re

import pytest

import lyric_looks as L
import lyric_recipes as R
import lyric_look_runtime as RT

SEGS = [{"text": "Y prefiero quebrarme a doblarme", "start": 1.0, "end": 4.0},
        {"text": "Lo compartió con su gente", "start": 4.2, "end": 7.0},
        {"text": "sí", "start": 7.1, "end": 7.3}]
GENRES = ["", "metal", "rock", "pop", "latin", "hiphop", "electronic", "ballad", "folk"]


def _dialogues(doc):
    return [d for d in doc.splitlines() if d.startswith("Dialogue:")]


# --- catalogue ------------------------------------------------------------------

def test_every_catalogue_font_exists_has_spanish_glyphs_and_a_license():
    from PIL import ImageFont
    fdir = L._FONTS_DIR
    lic = os.listdir(os.path.join(fdir, "licenses"))
    predates = ("Oswald", "Anton", "BebasNeue", "Montserrat", "Roboto", "Poppins",
                "Fredoka", "Jost", "Nunito", "Outfit", "Quicksand")
    for fid, (f, *_rest) in R.FONTS.items():
        path = os.path.join(fdir, f)
        assert os.path.exists(path), fid
        assert ImageFont.truetype(path, 40).getlength("ÑÁÉÍÓÚ¿¡") > 0, fid
        fam = f.split("-")[0]
        if fam not in predates:
            assert any(n.startswith(fam) for n in lic), fid


def test_catalog_shape():
    c = R.catalog()
    assert {f["id"] for f in c["families"]} == set(R.FAMILIES)
    assert all(g["family"] in R.FAMILIES for g in c["genres"])
    assert all(set(v) <= set(L.LOOKS) for v in c["recommended"].values())
    assert set(c["native"]) == set(L.LOOKS)


# --- determinism, originals, curated skins ---------------------------------------

def test_same_inputs_same_look():
    a = R.resolve("cinetico", genre="metal", variant=3, seed_key="job1")
    b = R.resolve("cinetico", genre="metal", variant=3, seed_key="job1")
    assert a[0] == b[0] and a[1] == b[1]


def test_native_variant_zero_is_the_original_design():
    for look_id, fam in R.NATIVE.items():
        genre = next(g for g, (f, _l) in R.GENRES.items() if f == fam)
        look, info = R.resolve(look_id, genre=genre, variant=0, seed_key="x")
        assert look == L.LOOKS[look_id], look_id
        assert info["description"] == "Diseño original"


def test_heavy_variant_zero_is_the_curated_skin():
    look, info = R.resolve("cinetico", genre="metal", variant=0, seed_key="x")
    assert look.key_font == "MetalMania-Regular.ttf" and look.background == "source"
    assert info["description"].startswith("Diseño curado")


def test_variants_change_with_the_song_and_the_number():
    sigs = {R.recipe_for("bloque", "metal", "job1", v).signature for v in range(8)}
    assert len(sigs) >= 6
    assert (R.recipe_for("bloque", "metal", "job1", 2).signature
            != R.recipe_for("bloque", "metal", "job2", 2).signature)


def test_motion_never_changes():
    for look_id, base in L.LOOKS.items():
        for g in ("metal", "pop", "hiphop", "ballad"):
            for v in range(3):
                look, _ = R.resolve(look_id, genre=g, variant=v, seed_key="s")
                assert (look.layout, look.motion) == (base.layout, base.motion)


def test_auto_variant_skips_sibling_signatures():
    used = {R.recipe_for("neon", "rock", "j", v).signature for v in range(3)}
    v = R.choose_auto_variant("neon", "rock", "j", used)
    assert R.recipe_for("neon", "rock", "j", v).signature not in used


# --- roles & readability ------------------------------------------------------------

def test_handwriting_script_and_chat_looks_keep_their_kind_of_face():
    for look_id, role in R.LOOK_TEXT_ROLE.items():
        for g in GENRES:
            for v in range(1, 4):
                rec = R.recipe_for(look_id, g, "s", v)
                if rec.original or rec.curated:
                    continue
                assert role in R.FONTS[rec.fonts[0]][5], (look_id, g, v, rec.fonts)


@pytest.mark.parametrize("genre", GENRES)
def test_accent_never_matches_the_grade_tint(genre):
    fam = R.family_of(genre) or R.NATIVE["bloque"]
    for v in range(1, 30):
        rec = R.recipe_for("bloque", genre, f"s{v}", v)
        if rec.original or rec.curated:
            continue
        assert not R._clashes(rec.colors[2], R.GRADES[fam][rec.grade][1])


def test_size_is_compensated_when_the_face_changes():
    base = L.LOOKS["pincel"]
    want = L.cap_per_fs(os.path.join(L._FONTS_DIR, base.font_file)) * base.font_scale
    look, _ = R.resolve("pincel", genre="metal", variant=1, seed_key="s",
                        overrides={"font_main": "bungee"})
    got = L.cap_per_fs(os.path.join(L._FONTS_DIR, look.font_file)) * look.font_scale
    assert got == pytest.approx(want, rel=0.02)


def test_duotone_lettering_never_uses_the_poster_colour():
    for v in range(1, 25):
        rec = R.recipe_for("duotono", "metal", f"s{v}", v)
        look = R.build_from_recipe(rec)
        assert rec.colors[2] not in look.line_colors


# --- operator overrides ---------------------------------------------------------------

def test_overrides_apply_on_top_and_are_reported():
    look, info = R.resolve("cinetico", genre="metal", variant=2, seed_key="s",
                           overrides={"font_main": "anton", "font_key": "pirata-one",
                                      "ink": "#FFEEAA", "accent": "#00FF88"})
    assert look.font_file == "Anton-Regular.ttf" and look.key_font == "PirataOne-Regular.ttf"
    assert look.line_colors[0] == "#FFEEAA" and look.line_colors[2] == "#00FF88"
    assert info["overridden"] == {"font_main": True, "font_key": True, "ink": True,
                                  "alt": False, "accent": True}
    assert info["colors"]["ink"] == "#FFEEAA"


def test_wizard_white_and_garbage_are_not_overrides():
    _l, info = R.resolve("cine", genre="ballad", variant=1, seed_key="s",
                         overrides={"ink": "#ffffff", "font_main": "comic-sans", "alt": "red"})
    assert not any(info["overridden"].values())


def test_dark_text_override_warns():
    _l, info = R.resolve("cine", genre="ballad", variant=1, seed_key="s",
                         overrides={"ink": "#151515"})
    assert "low_contrast_ink" in info["warnings"]


def test_overriding_pop70_accent_drops_card_accents():
    look, _ = R.resolve("pop70", genre="pop", variant=0, seed_key="s",
                        overrides={"accent": "#123456"})
    assert look.flat_accents == () and look.accent == "#123456"


# --- every variant renders --------------------------------------------------------------

@pytest.mark.parametrize("look_id", sorted(L.LOOKS))
def test_every_look_genre_variant_renders_valid_ass(look_id):
    for g in ("", "metal", "pop", "hiphop", "ballad"):
        for v in (0, 1, 2):
            look, _ = R.resolve(look_id, genre=g, variant=v, seed_key="seed")
            for w, h in ((1920, 1080), (1080, 1920)):
                doc = L.build_look_ass(SEGS, look, width=w, height=h, duration=8,
                                       beats=[1.5, 2.0, 2.5, 3.0, 5.0, 6.0])
                events = _dialogues(doc)
                assert events, (look_id, g, v)
                for d in events:
                    assert d.count("{") == d.count("}")
                    for a, b in re.findall(r"\\t\((-?\d+),(-?\d+),", d):
                        assert 0 <= int(a) < int(b), (look_id, g, v, d[:80])


# --- runtime --------------------------------------------------------------------------

def test_clean_fields_validates_everything():
    out = RT.clean_fields(lyric_genre="METAL", lyric_variant="3", lyric_font_main="anton",
                          lyric_font_key="nope", lyric_alt_color="#abcdef",
                          lyric_accent_color="blue")
    assert out == {"lyric_genre": "metal", "lyric_variant": 3,
                   "lyric_font_main": "anton", "lyric_alt_color": "#ABCDEF"}
    assert RT.clean_fields(lyric_variant="") == {}
    assert RT.clean_fields(lyric_variant=-1) == {}


def test_prepare_opts_respects_operator_then_persisted_then_detects(monkeypatch):
    monkeypatch.setattr(RT, "sibling_signatures", lambda j, l: set())
    import genre_detect
    calls = []
    monkeypatch.setattr(genre_detect, "detect",
                        lambda a, t, l="", job_id=None: calls.append(1) or ("metal", "ai"))
    opts, upd = RT.prepare_opts(job_id="j1", lyric_look="cinetico", artist="Almafuerte",
                                song_title="Niño Jefe", render_params={})
    assert opts["genre"] == "metal" and upd["lyric_genre_detected"] == "metal"
    assert isinstance(upd["lyric_variant"], int) and "lyric_variant_sig" in upd
    opts2, upd2 = RT.prepare_opts(job_id="j1", lyric_look="cinetico",
                                  render_params={"lyric_genre": "pop", "lyric_variant": 4})
    assert opts2["genre"] == "pop" and opts2["variant"] == 4 and upd2["lyric_variant"] == 4
    assert len(calls) == 1


def test_resolve_for_render_matches_resolve():
    opts = {"genre": "metal", "variant": 2, "seed_key": "j", "font_main": "", "font_key": "",
            "ink": "", "alt": "", "accent": "#00FF88"}
    a = RT.resolve_for_render("bloque", opts)
    b, _ = R.resolve("bloque", genre="metal", variant=2, seed_key="j",
                     overrides={"accent": "#00FF88"})
    assert a == b


def test_owns_background_depends_on_the_resolved_variant():
    assert RT.owns_background("cinetico", {"genre": "pop", "variant": 0, "seed_key": "j"})
    assert not RT.owns_background("cinetico", {"genre": "metal", "variant": 0, "seed_key": "j"})


def test_genre_answer_parsing():
    import genre_detect
    assert genre_detect.parse_answer("Metal.") == "metal"
    assert genre_detect.parse_answer("hip-hop") == "hiphop"
    assert genre_detect.parse_answer("cumbia") == "latin"      # the prompt says so
    assert genre_detect.parse_answer("latino") == "latin"
    # Compounds: the last genre word decides, with or without a hyphen.
    for answer, genre in (("pop rock", "rock"), ("Pop-Rock", "rock"), ("Pop punk", "punk"),
                          ("folk rock", "rock"), ("rap metal", "metal"), ("latin pop", "latin"),
                          ("metalcore", "metal"), ("Rock, but also pop", "rock"),
                          ("N/A", ""), ("R&B", ""), ("alternative", "")):
        assert genre_detect.parse_answer(answer) == genre, answer


def test_genre_detection_never_raises(monkeypatch):
    import genre_detect

    class Boom:
        def generate_text(self, *a, **k):
            raise RuntimeError("quota")
    monkeypatch.setattr(genre_detect, "_MEM", {})
    import storage
    monkeypatch.setattr(storage, "is_enabled", lambda: False)
    assert genre_detect.detect("X", "Y", provider=Boom()) == ("", "none")


# --- regresiones de la revisión ---------------------------------------------------

def test_preview_and_render_share_the_song_seed(monkeypatch):
    monkeypatch.setattr(RT, "sibling_signatures", lambda j, l: set())
    opts, _ = RT.prepare_opts(job_id="any-job-id", lyric_look="bloque", artist="La Renga",
                              song_title="Blues Cardíaco",
                              render_params={"lyric_genre": "rock", "lyric_variant": 2})
    render = RT.resolve_for_render("bloque", opts)
    preview, _ = R.resolve("bloque", genre="rock", variant=2,
                           seed_key=R.seed_key_for("  la renga ", "Blues  Cardíaco"))
    assert render == preview


def test_auto_variants_spread_across_songs_without_siblings():
    songs = [R.seed_key_for("Artista", f"Canción {i}") for i in range(12)]
    picked = {R.choose_auto_variant("cinetico", "metal", s) for s in songs}
    assert len(picked) >= 4


def test_main_font_override_keeps_the_second_face():
    neon, info = R.resolve("neon", genre="", variant=0, seed_key="s",
                           overrides={"font_main": "anton"})
    assert neon.font_file == "Anton-Regular.ttf"
    assert neon.extra_fonts == ("Neonderthaw-Regular.ttf",)
    assert info["fonts"]["key"]["id"] == "neonderthaw"
    bloque, _ = R.resolve("bloque", genre="", variant=0, seed_key="s",
                          overrides={"font_main": "anton"})
    assert bloque.extra_fonts == ("BigShouldersDisplay-Light.ttf",)


def test_genre_answers_with_hyphens_and_accents():
    import genre_detect
    assert genre_detect.parse_answer("folk-rock") == "rock"
    assert genre_detect.parse_answer("Pop-punk") == "punk"
    assert genre_detect.parse_answer("K-pop") == "pop"
    assert genre_detect.parse_answer("Reggaetón") == "reggaeton"


def test_campaign_accepts_automatic_colours_and_numeric_variants():
    import campaign_creative as cc
    out = cc.normalize_settings({"lyric_alt_color": "", "lyric_accent_color": "#FF0000",
                                 "lyric_variant": 3, "lyric_genre": "metal"})
    assert out["lyric_alt_color"] == "" and out["lyric_variant"] == "3"
    assert cc.normalize_settings({"lyric_variant": ""})["lyric_variant"] == ""
    assert cc.normalize_settings({"lyric_variant": "25"})["lyric_variant"] == "25"


def test_veo_group_allows_cinetico_only_where_it_uses_the_song_background():
    import campaign_creative as cc
    from fastapi import HTTPException
    g = cc.Group(id="g", name="Veo", weight=100, requirement="veo", model=cc.VEO_LITE)

    class _C:
        tenant_id = "t"
    # Rock/metal Cinético composes over the song's background → Veo is seen.
    cc.validate_combination(None, _C(), {"lyric_look": "cinetico", "lyric_genre": "metal",
                                         "movement_style": "estandar"}, g, {})
    # Genre "" (detected per song) or pop: its original design paints the
    # whole frame, so the Veo background would be skipped/hidden.
    for genre in ("", "pop"):
        with pytest.raises(HTTPException):
            cc.validate_combination(None, _C(), {"lyric_look": "cinetico", "lyric_genre": genre,
                                                 "movement_style": "estandar"}, g, {})
    assert not R.may_own_background("cine", "") and R.may_own_background("pop70", "metal")
    with pytest.raises(HTTPException):
        cc.validate_combination(None, _C(), {"lyric_look": "pop70",
                                             "movement_style": "estandar"}, g, {})


def test_legacy_flag_keeps_the_look_as_designed_on_every_edit(monkeypatch):
    import genre_detect
    monkeypatch.setattr(genre_detect, "detect", lambda *a, **k: ("metal", "ai"))
    monkeypatch.setattr(RT, "sibling_signatures", lambda *a: set())
    opts, upd = RT.prepare_opts(job_id="j", lyric_look="cinetico", artist="Almafuerte",
                                song_title="Niño Jefe",
                                render_params={"lyric_look_legacy": True, "lyric_variant": 0,
                                               "lyric_variant_sig": ["x"],
                                               "lyric_color": "#19E0BC"})
    assert opts["legacy"] and opts["genre"] == "" and opts["variant"] == 0
    assert upd["lyric_look_legacy"] is True
    look = RT.resolve_for_render("cinetico", opts)
    assert look == L.LOOKS["cinetico"]                      # exactly as designed
    # …and the text colour is applied the old way (on top of the look).
    assert RT.primary_override(opts, "#19E0BC") == "#19E0BC"
    fresh, _ = RT.prepare_opts(job_id="j", lyric_look="cinetico", artist="Almafuerte",
                               song_title="Niño Jefe", render_params={"lyric_color": "#19E0BC"})
    assert RT.primary_override(fresh, "#19E0BC") == ""     # lives in the resolved look
    assert RT.primary_override({}, "#19E0BC") == "#19E0BC"  # preparation failed


def test_short_and_master_apply_the_same_text_colour_when_opts_failed():
    import pipeline
    doc = pipeline._build_short_ass_doc(
        [{"start": 0.0, "end": 3.0, "text": "hola"}],
        font_path=L.font_path(L.LOOKS["cine"]), text_case="upper", font_scale=1.0,
        lyric_color="#19E0BC", lyric_sung_color="", text_contrast="medium",
        lyrics_animation="none", line_transition="none", lyric_look="cine",
        look_obj=L.LOOKS["cine"], look_primary_override=RT.primary_override({}, "#19E0BC"))
    assert "&H00BCE019" in doc


def test_libass_gets_the_family_name_it_can_match():
    # libass matches name ID 1; Pillow reports the typographic family (ID 16),
    # so "Montserrat" fell back to DejaVu Sans in every title card.
    import ass_render
    fonts = os.path.join(os.path.dirname(L.__file__), "fonts")
    assert ass_render.font_family(os.path.join(fonts, "Montserrat-ExtraBold.ttf"))[0] == "Montserrat ExtraBold"
    assert ass_render.font_family(os.path.join(fonts, "Fredoka-SemiBold.ttf"))[0] == "Fredoka SemiBold"
    assert ass_render.font_family(os.path.join(fonts, "Anton-Regular.ttf")) == ("Anton", False)


def test_chat_colour_overrides_keep_every_message_readable():
    look, info = R.resolve("chat", variant=0, overrides={"ink": "#FF0000"})
    assert look.flat_colors[1] != "#FFFFFF" and look.accent == "#FFFFFF"   # outgoing readable
    look, info = R.resolve("chat", variant=3, overrides={"accent": "#00AA00"})
    assert look.primary == info["colors"]["ink"] != "#FFFFFF"              # incoming stays dark
    assert look.flat_colors == (info["colors"]["alt"], "#00AA00")
    look, _ = R.resolve("chat", overrides={"accent": "#FFE680"})
    assert look.accent == "#111111"                                         # dark text on light bubble
    _, info = R.resolve("chat", overrides={"ink": "#DDDDDD"})
    assert "low_contrast_ink" in info["warnings"]


@pytest.mark.parametrize("look_id", sorted(R._FONT_ONLY_RECIPES))
def test_typography_only_variants_never_repeat_a_font_pair(look_id):
    for genre in ("", "metal", "hiphop", "ballad"):
        recs = [R.recipe_for(look_id, genre, "a|b", v) for v in range(0, 12)]
        sigs = [r.signature for r in recs[1:]]
        # Every distinct pair is used before any repeats (chat has only a
        # handful of UI faces, so it cycles).
        n = len(set(sigs))
        assert n >= 2 and len(set(sigs[:n])) == n, (genre, sigs)
        assert all(a != b for a, b in zip(sigs, sigs[1:]))
        for r in recs:
            if not (r.original or r.curated):
                assert r.palette == R.FONTS_ONLY_PALETTE and "colores del diseño" in r.describe()


def test_cascade_key_word_lands_whole_and_inside_the_frame():
    look, _ = R.resolve("cinetico", variant=1, seed_key="s|t")
    assert look.cascade
    hit = False
    for f1 in ("a", "de", "mi", "tu", "la", "el", "y", "en", "su", "no", "ya", "yo"):
        for f2 in ("se", "lo", "me", "te", "al", "va", "si", "es"):
            doc = L.build_look_ass([{"start": 0.0, "end": 1.0,
                                     "text": f"{f1} {f2} INTERNACIONALIZACIÓN"}], look,
                                   width=1080, height=1920, duration=1.0)
            evs = [d for d in _dialogues(doc) if "\\an1\\move" in d]
            if not evs:
                continue
            hit = True
            assert "".join(e.rsplit("}", 1)[1] for e in evs) == "INTERNACIONALIZACIÓN"
            assert all("\\fn" in e for e in evs)                # the key face
            xs = [float(re.search(r"\\move\([^,]+,[^,]+,([-\d.]+)", e).group(1)) for e in evs]
            assert min(xs) >= 0 and max(xs) < 1080
    assert hit
