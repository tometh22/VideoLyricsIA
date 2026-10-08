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
    # Oswald predates the licenses folder; every font added for looks has one.
    if family != "Oswald":
        assert any(n.startswith(family) for n in licenses)


@pytest.mark.parametrize("look_id", sorted(L.LOOKS))
def test_every_look_keeps_lyrics_inside_their_window(look_id):
    doc = L.build_look_ass(SEGS, L.LOOKS[look_id], width=1920, height=1080,
                           duration=8.0)
    lyric_events = [d for d in _dialogues(doc) if "\\p1" not in d]
    assert lyric_events
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
