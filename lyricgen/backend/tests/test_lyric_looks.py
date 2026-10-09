import os
import re

import pytest

import fx_compositor
import lyric_looks as L

SEGS = [
    {"text": "Tumbado en un callejón", "start": 0.8, "end": 3.8,
     "words": [{"word": "Tumbado", "start": 0.88, "end": 1.48},
               {"word": "en", "start": 1.52, "end": 1.64},
               {"word": "un", "start": 1.68, "end": 1.78},
               {"word": "callejón", "start": 1.9, "end": 3.3}]},
    {"text": "Lluvia espacial", "start": 4.0, "end": 7.0},
]


def _dialogues(doc):
    return [l for l in doc.splitlines() if l.startswith("Dialogue:")]


def _secs(ts):
    h, m, s = ts.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


@pytest.mark.parametrize("look_id", sorted(L.LOOKS))
def test_every_look_ships_its_font_and_license(look_id):
    look = L.LOOKS[look_id]
    assert os.path.exists(L.font_path(look))
    family = look.font_file.split("-")[0]
    licenses = os.listdir(os.path.join(os.path.dirname(L.font_path(look)), "licenses"))
    # Catalogue fonts that predate the licenses folder; every font added for
    # looks ships its license next to it.
    if family not in ("Oswald", "Montserrat", "Roboto"):
        assert any(n.startswith(family) for n in licenses)


@pytest.mark.parametrize("look_id", sorted(L.LOOKS))
def test_every_look_keeps_lyrics_inside_their_window(look_id):
    doc = L.build_look_ass(SEGS, L.LOOKS[look_id], width=1920, height=1080,
                           duration=8.0)
    lyric_events = [d for d in _dialogues(doc) if "\\p1" not in d]
    assert lyric_events
    if look_id == "chat":
        # A conversation keeps earlier bubbles on screen (scrolling up);
        # what must hold is that each bubble ARRIVES at its line's start.
        firsts = sorted({_secs(d.split(",")[1]) for d in lyric_events})
        assert firsts[0] == pytest.approx(SEGS[0]["start"], abs=0.01)
        assert any(abs(f - SEGS[1]["start"]) < 0.01 for f in firsts)
        return
    for d in lyric_events:
        _, start, end = d.split(",")[:3]
        start, end = _secs(start), _secs(end)
        inside = [(s["start"], s["end"]) for s in SEGS
                  if s["start"] - 0.05 <= start and end <= s["end"] + 0.01]
        assert inside, d


def test_pick_keyword_takes_last_content_word():
    assert L.pick_keyword("LAVARÁ SUS HERIDAS".split()) == 2
    assert L.pick_keyword("PRONTO SE IRÁ".split()) == 0
    assert L.pick_keyword("Y SE FUE".split()) is None


def test_keyword_layout_puts_small_lead_over_big_key():
    doc = L.build_look_ass(SEGS[:1], L.LOOKS["cine"], width=1920, height=1080,
                           duration=4.0)
    line = [d for d in _dialogues(doc) if "CALLEJÓN" in d][0]
    lead_fs, key_fs = map(int, re.findall(r"\\fs(\d+)", line)[:2])
    assert "TUMBADO EN UN\\N" in line and key_fs > 2 * lead_fs


def test_build_layout_lands_each_word_at_its_sung_time_without_overlap():
    doc = L.build_look_ass(SEGS[:1], L.LOOKS["pincel"], width=1920, height=1080,
                           duration=4.0)
    words = _dialogues(doc)
    assert len(words) == 4
    starts = [_secs(d.split(",")[1]) for d in words]
    assert starts == sorted(starts) and starts[3] == pytest.approx(1.86, abs=0.01)
    xs = [int(re.search(r"\\pos\((\d+),", d).group(1)) for d in words[:3]]
    assert xs == sorted(xs) and min(b - a for a, b in zip(xs, xs[1:])) > 60
    assert "\\1c&H002F26E3" in words[3]           # key word in red


def test_flat_cards_cover_the_whole_song_and_cut_on_each_line():
    doc = L.build_look_ass(SEGS, L.LOOKS["pop70"], width=1920, height=1080,
                           duration=8.0)
    cards = [d for d in _dialogues(doc) if "\\p1" in d]
    spans = [(_secs(c.split(",")[1]), _secs(c.split(",")[2])) for c in cards]
    assert spans == [(0.0, 4.0), (4.0, 8.0)]


def test_cine_draws_letterbox_bars():
    doc = L.build_look_ass(SEGS, L.LOOKS["cine"], width=1920, height=1080,
                           duration=8.0)
    bars = [d for d in _dialogues(doc) if "\\p1" in d]
    assert len(bars) == 2 and all(d.startswith("Dialogue: 5,0:00:00.00,0:00:08.00") for d in bars)


def test_build_video_filter_uses_the_look_grade():
    vf, use_complex, _ = fx_compositor.build_video_filter(
        ass_basename="l.ass", font_dir="/f", width=1920, height=1080,
        style="neon", grade_override="eq=saturation=0.5")
    assert not use_complex and vf.startswith("eq=saturation=0.5,subtitles=")


def test_operator_colour_overrides_look_but_wizard_white_does_not():
    look = L.LOOKS["pelicula"]
    keep = L.build_look_ass(SEGS, look, width=1920, height=1080, duration=8.0,
                            primary_override="#ffffff")
    over = L.build_look_ass(SEGS, look, width=1920, height=1080, duration=8.0,
                            primary_override="#19E0BC")
    style = lambda doc: [l for l in doc.splitlines() if l.startswith("Style:")][0]
    assert "&H0030C2F2" in style(keep)            # look's yellow
    assert "&H00BCE019" in style(over)            # operator's colour


def test_title_card_is_drawn_above_the_look():
    import ass_render
    title = ass_render.title_card_lines(
        "Enanitos Verdes", "El País Del No Dormir", 5.0, width=1920, height=1080,
        text_scale=1.0, lyric_font_family="Bagel", artist_font_family="Bagel")
    doc = L.build_look_ass(SEGS, L.LOOKS["pop70"], width=1920, height=1080,
                           duration=8.0, title_lines=title)
    titles = [d for d in _dialogues(doc) if "Enanitos" in d or "ENANITOS" in d]
    assert titles and all(d.startswith("Dialogue: 6,") for d in titles)


def test_vertical_frame_has_no_letterbox():
    doc = L.build_look_ass(SEGS, L.LOOKS["cine"], width=1080, height=1920,
                           duration=8.0)
    assert not [d for d in _dialogues(doc) if "\\p1" in d]


def test_short_doc_uses_the_look():
    import pipeline
    doc = pipeline._build_short_ass_doc(
        [{"start": 0.0, "end": 3.0, "text": "lavará sus heridas"}],
        font_path=L.font_path(L.LOOKS["cine"]), text_case="upper",
        font_scale=1.0, lyric_color="", lyric_sung_color="",
        text_contrast="medium", lyrics_animation="none",
        line_transition="none", lyric_look="cine",
    )
    assert "PlayResX: 1080" in doc and "Marcellus" in doc
    assert "LAVARÁ SUS\\N" in doc


def test_api_only_forwards_a_known_look():
    import main
    assert main._lyric_look_kwargs("") == {}
    assert main._lyric_look_kwargs("nope") == {}
    assert main._lyric_look_kwargs(" Cine ") == {"lyric_look": "cine"}


def test_batch_profile_accepts_a_look_and_keeps_old_profiles_identical():
    import batch_profiles as bp
    base = {"font": "anton", "font_scale": 1.0}
    assert "lyric_look" not in bp.pipeline_fields(bp.normalize_render_profile(base))
    prof = bp.normalize_render_profile({**base, "lyric_look": "pincel"})
    assert bp.pipeline_fields(prof)["lyric_look"] == "pincel"
    with pytest.raises(bp.RenderProfileError):
        bp.normalize_render_profile({**base, "lyric_look": "glitter"})


def test_campaigns_expose_every_look_and_default_to_none():
    import campaign_creative as cc
    assert cc.FIELDS["lyric_look"]["options"] == ["", *L.LOOKS]
    assert "lyric_look" in cc.RENDER_KEYS        # viaja en settings → /generate

    class _Campaign:
        default_render_params = {"lyric_look": "cine"}

    assert cc.effective_settings(type("C", (), {"default_render_params": {}})(), {})["lyric_look"] == ""
    assert cc.effective_settings(_Campaign(), {})["lyric_look"] == "cine"
    assert cc.effective_settings(_Campaign(), {"lyric_look": "pop70"})["lyric_look"] == "pop70"


def test_measure_uses_libass_win_metrics():
    # Big Shoulders: hhea spans 1.20 em, OS/2 win spans 1.67 em. libass sizes
    # by the latter; measuring with hhea put cascade letters 28% apart.
    em = L._em_per_fs(os.path.join(os.path.dirname(L.__file__), "fonts",
                                   "BigShouldersDisplay-Black.ttf"))
    assert em == pytest.approx(2000 / (2614 + 730), abs=0.002)


def test_kinetic_lands_words_as_sung_and_kicks_on_the_beat():
    look = L.LOOKS["cinetico"]
    doc = L.build_look_ass(SEGS[:1], look, width=1920, height=1080, duration=4.0,
                           beats=[0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5])
    words = [d for d in _dialogues(doc) if d.startswith("Dialogue: 3,")]
    starts = sorted(_secs(d.split(",")[1]) for d in words)
    assert starts[0] == pytest.approx(0.83, abs=0.02)      # "TUMBADO" as sung
    assert any("\\fscx110" in d for d in words)            # key word beat kick
    assert any("\\fnCaveat" in d for d in words)           # script connector


def test_neon_sign_frame_never_fills_its_box():
    doc = L.build_look_ass(SEGS[:1], L.LOOKS["neon"], width=1920, height=1080,
                           duration=4.0, beats=[1.0, 2.0, 3.0])
    frames = [d for d in _dialogues(doc) if "\\p1" in d]
    assert frames and all("\\alpha" not in d.split("\\p1")[0] for d in frames)


def test_looks_that_paint_their_own_frame_skip_the_background():
    assert L.owns_background("cinetico") and L.owns_background("pop70")
    assert L.owns_background("degrade")
    assert not L.owns_background("cine") and not L.owns_background("")


def test_background_prompt_is_steered_by_the_look():
    import pipeline
    base = "A quiet harbour at dusk."
    assert pipeline._steer_prompt_for_look(base, "") == base
    assert pipeline._steer_prompt_for_look(base, "pop70") == base      # no hint
    arco = pipeline._steer_prompt_for_look(base, "arco")
    assert arco.startswith(base) and "empty centre" in arco
    assert "night" not in pipeline._steer_prompt_for_look(base, "neon")


def test_look_beats_are_rebased_to_the_short_window(monkeypatch):
    import pipeline, beat_snap
    monkeypatch.setattr(beat_snap, "detect_beats", lambda p: (120.0, [1.0, 30.5, 31.0]))
    assert pipeline._look_beats(L.LOOKS["neon"], "song.wav", offset=30.0) == [0.5, 1.0]
    assert pipeline._look_beats(L.LOOKS["cine"], "song.wav") == []    # no beat_sync
    monkeypatch.setattr(beat_snap, "detect_beats", lambda p: None)
    assert pipeline._look_beats(L.LOOKS["neon"], "song.wav") == []


def test_every_look_font_is_discoverable_by_libass():
    import ass_render
    for look in L.LOOKS.values():
        for path in L.font_paths(look):
            family, _bold = ass_render.font_family(path)
            assert family and os.path.exists(path), path


def test_campaign_rejects_background_owning_look_in_a_background_group():
    import campaign_creative as cc
    from fastapi import HTTPException
    group = cc.Group(id="g", name="Veo", weight=100, requirement="veo", model=cc.VEO_LITE)

    class _C:
        tenant_id = "t"
    with pytest.raises(HTTPException):
        cc.validate_combination(None, _C(), {"lyric_look": "pop70",
                                             "movement_style": "estandar"}, group, {})


# --- Regresiones de la revisión del 8-oct ----------------------------------

def _t_windows(doc):
    import re
    out = []
    for d in _dialogues(doc):
        dur = int(round((_secs(d.split(",")[2]) - _secs(d.split(",")[1])) * 1000))
        for a, b in re.findall(r"\\t\((-?\d+),(-?\d+),", d):
            out.append((int(a), int(b), dur, d[:60]))
    return out


def test_chat_survives_overlapping_and_out_of_order_lines():
    segs = [{"text": "tres cuatro", "start": 1.0, "end": 3.5},
            {"text": "uno dos", "start": 1.0, "end": 3.0},
            {"text": "cinco", "start": 0.5, "end": 2.0}]
    doc = L.build_look_ass(segs, L.LOOKS["chat"], width=1920, height=1080, duration=5)
    assert len(_dialogues(doc)) >= 3


def test_vertical_short_uses_the_same_type_size_as_the_master():
    import re
    segs = [{"text": "y yo te sigo esperando bajo la lluvia", "start": 1, "end": 4}]
    for lid, look in L.LOOKS.items():
        if look.layout in ("block", "kinetic"):
            continue          # rows are stretched to the frame width on purpose
        fs = lambda w, h: max(int(x) for x in re.findall(
            r"\\fs(\d+)", L.build_look_ass(segs, look, width=w, height=h, duration=6)))
        assert fs(1080, 1920) <= fs(1920, 1080) * 1.05, lid


@pytest.mark.parametrize("look_id", sorted(L.LOOKS))
def test_animations_stay_inside_their_event_even_on_tiny_lines(look_id):
    segs = [{"text": "sí", "start": 1.0, "end": 1.12},
            {"text": "te quiero mucho mi amor", "start": 2.0, "end": 2.3,
             "words": [{"word": "te", "start": 2.0, "end": 2.05},
                       {"word": "quiero", "start": 2.05, "end": 2.1},
                       {"word": "mucho", "start": 2.1, "end": 2.2},
                       {"word": "mi", "start": 2.2, "end": 2.25},
                       {"word": "amor", "start": 2.28, "end": 2.3}]}]
    doc = L.build_look_ass(segs, L.LOOKS[look_id], width=1920, height=1080,
                           duration=4, beats=[1.05, 2.1, 2.2])
    for d in _dialogues(doc):
        assert _secs(d.split(",")[2]) > _secs(d.split(",")[1]), d[:80]
    for a, b, dur, d in _t_windows(doc):
        assert 0 <= a < b <= max(dur, 2), (a, b, dur, d)


def test_cuaderno_ring_stays_on_screen():
    segs = [{"text": "te quiero mucho mi amor", "start": 1.0, "end": 3.0}]
    doc = L.build_look_ass(segs, L.LOOKS["cuaderno"], width=1920, height=1080, duration=4)
    rings = [d for d in _dialogues(doc) if "\\p1" in d and "E8322E".lower() in d.lower()
             or ("\\p1" in d and "&H002E32E8" in d)]
    assert rings
    for d in rings:
        assert _secs(d.split(",")[2]) - _secs(d.split(",")[1]) >= 0.55


def test_word_timed_looks_request_forced_alignment():
    for lid in ("pincel", "pop70", "cuaderno", "y2k", "cinetico", "bloque", "arco"):
        assert L.needs_word_timings(lid), lid
    for lid in ("cosmico", "cine", "neon", "chat", "duotono", ""):
        assert not L.needs_word_timings(lid), lid


def test_short_look_background_covers_the_whole_short():
    import pipeline
    doc = pipeline._build_short_ass_doc(
        [{"start": 0.0, "end": 3.0, "text": "lavará sus heridas"}],
        font_path=L.font_path(L.LOOKS["degrade"]), text_case="upper",
        font_scale=1.0, lyric_color="", lyric_sung_color="", text_contrast="medium",
        lyrics_animation="none", line_transition="none", lyric_look="degrade",
        duration=30.0)
    bands = [d for d in _dialogues(doc) if d.startswith("Dialogue: 0,")]
    assert bands and all(d.split(",")[2] == "0:00:30.00" for d in bands)


def test_title_card_is_readable_on_dark_looks():
    import ass_render
    title = ass_render.title_card_lines(
        "Enanitos Verdes", "El País Del No Dormir", 5.0, width=1920, height=1080,
        text_scale=1.0, lyric_font_family="Roboto", artist_font_family="Roboto")
    doc = L.build_look_ass(SEGS, L.LOOKS["chat"], width=1920, height=1080,
                           duration=8.0, title_lines=title)
    style = [l for l in doc.splitlines() if l.startswith("Style:")][0]
    titles = [d for d in _dialogues(doc) if d.startswith("Dialogue: 6,")]
    assert titles and all("&H00111111" not in t for t in titles)
