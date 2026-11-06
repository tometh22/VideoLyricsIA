"""Contracts of the approved video overlays, including sparse/late word timing."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import re

import pytest
import ass_render as A
import lyric_looks as L
import lyric_recipes as R
from lyric_editorial import EditorialRenderer, _scale_text

IDS = L.EDITORIAL_LAYOUTS
SEGMENTS = [
    {"text": "El que yo conocí como el jefe", "start": 1, "end": 4,
     "words": [{"word": w, "start": t, "end": t + .06} for w, t in
               zip("El que yo conocí como el jefe".split(), [1.2, 1.28, 1.36, 2, 2.8, 3.1, 3.86])]},
    {"text": "Cae", "start": 5, "end": 5.08,
     "words": [{"word": "Cae", "start": 5.04, "end": 5.08}]},
]


def seconds(t):
    h, m, s = map(float, t.split(':'))
    return h*3600+m*60+s


@pytest.mark.parametrize('code', IDS)
@pytest.mark.parametrize('size', [(1280, 720), (1920, 1080), (1080, 1920)])
def test_real_word_windows_are_kept_in_both_formats(code, size):
    before = deepcopy(SEGMENTS)
    doc = L.build_look_ass(SEGMENTS, L.LOOKS[code], width=size[0], height=size[1], duration=6)
    assert SEGMENTS == before
    assert L.needs_word_timings(code)
    assert not L.owns_background(code)
    assert L.LOOKS[code].background == 'source'
    events = [e for e in doc.splitlines() if e.startswith('Dialogue:') and r'\p1' not in e]
    assert events
    for e in events:
        start, end = map(seconds, e.split(',')[1:3])
        assert any(s['start'] <= start < end <= s['end']+.01 for s in SEGMENTS)
        for a, b in re.findall(r'\\t\((\d+),(\d+),', e):
            assert 0 <= int(a) < int(b) <= round((end-start)*1000)+10
    keyword = [e for e in events if 'jefe' in e.lower()]
    assert keyword and all(seconds(e.split(',')[1]) >= 3.82 for e in keyword)


@pytest.mark.parametrize('code', IDS)
def test_default_selection_preserves_approved_design_in_every_genre(code):
    for genre in ['', 'rock', 'pop', 'ballad', 'electronic']:
        for seed in ['one', 'another']:
            assert R.choose_auto_variant(code, genre, seed, {('already used',)}) == 0
            look, info = R.resolve(code, genre=genre, seed_key=seed, variant=0)
            assert look == L.LOOKS[code]
            assert info['background'] == 'source'
            assert info['fonts']['main']['id'] in R.FONTS
        changed, _ = R.resolve(code, genre=genre, seed_key='one', variant=1)
        assert changed.layout == L.LOOKS[code].layout


@pytest.mark.parametrize('code', IDS)
def test_operator_key_font_and_accent_reach_the_actual_glyphs(code):
    look, _ = R.resolve(code, variant=0, overrides={'font_key': 'bungee', 'accent': '#1234AB'})
    doc = L.build_look_ass(SEGMENTS[:1], look, width=1280, height=720, duration=5)
    events = [e for e in doc.splitlines() if e.startswith('Dialogue:') and 'jefe' in e.lower()]
    assert any(r'\fnBungee' in e for e in events)
    if code == 'recorte':
        # Recorte uses the accent for the paper, with contrasting ink on top.
        assert any(r'\p1' in e and L._c('#1234AB') in e for e in doc.splitlines())
    else:
        assert any(L._c('#1234AB') in e for e in events)


def test_short_side_scaling_moves_geometry_but_not_time_or_lyric_numbers():
    raw = r'{\pos(100,200)\fs40\bord2\clip(1,2,3,4)\t(0,90,\fscx100)}1984'
    scaled = _scale_text(raw, 1.5)
    assert r'\pos(150,300)' in scaled and r'\fs60' in scaled
    assert r'\t(0,90,\fscx100)' in scaled and scaled.endswith('1984')
    shape = _scale_text(r'{\pos(0,0)\p1}m 10 20 l 30 40', 1.5)
    assert shape.endswith('m 15 30 l 45 60')


def test_puntada_uses_visible_glyph_height_and_has_compact_spacing():
    renderer = EditorialRenderer(L.LOOKS['puntada'], 1280, 720, 6)
    positions = []
    real = renderer.text
    def capture(layer, start, end, body, x, y, fs, font, color, *a, **kw):
        if layer == 4:
            positions.append((body, y, renderer.measures[font].ink_height(body, fs)))
        return real(layer, start, end, body, x, y, fs, font, color, *a, **kw)
    renderer.text = capture
    renderer.puntada({**SEGMENTS[0], 'display': SEGMENTS[0]['text'].lower()}, 0)
    rows = {}
    for _, y, ink in positions:
        rows[y] = max(rows.get(y, 0), ink)
    assert len(rows) == 3
    ys = sorted(rows)
    assert ys[-1] - ys[0] < 175  # previous line-box spacing was >220px
    for a, b in zip(ys, ys[1:]):
        assert 12 <= (b-a)-(rows[a]+rows[b])/2 <= 24


def test_parallel_renders_do_not_share_events_fonts_or_geometry():
    def render(pair):
        code, width, height = pair
        return L.build_look_ass(SEGMENTS, L.LOOKS[code], width=width, height=height, duration=6)
    jobs = [(code, w, h) for code in IDS for w, h in [(1280, 720), (1080, 1920)]]
    expected = [render(job) for job in jobs]
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(render, jobs)) == expected


@pytest.mark.parametrize('code', IDS)
def test_late_alignment_does_not_show_a_word_at_the_line_start(code):
    seg = {'text': 'tarde', 'start': 1, 'end': 2, 'words': [{'word': 'tarde', 'start': 8, 'end': 9}]}
    doc = L.build_look_ass([seg], L.LOOKS[code], width=1280, height=720, duration=3)
    assert not any('tarde' in e.lower() for e in doc.splitlines() if e.startswith('Dialogue:'))


@pytest.mark.parametrize('code', IDS)
def test_long_words_and_font_size_control_fit_inside_vertical_frame(code):
    renderer = EditorialRenderer(L.LOOKS[code], 1080, 1920, 8, font_scale=1.5)
    captured = []
    real = renderer.text
    def capture(layer, start, end, body, x, y, fs, font, color, extra='', align=5):
        width = renderer.measures[font].width(body, fs)
        left = x if align == 4 else x-width/2
        captured.append((left, left+width, body))
        return real(layer, start, end, body, x, y, fs, font, color, extra, align)
    renderer.text = capture
    display = 'extraordinariamente yo sigo aquí esperando'
    getattr(renderer, code)({'display': display, 'text': display, 'start': 1, 'end': 7}, 0)
    assert captured
    for left, right, body in captured:
        assert -1 <= left < right <= renderer.w+1, (code, body, left, right)


def test_short_syllable_fades_have_a_fully_visible_interval():
    r = EditorialRenderer(L.LOOKS['puntada'], 1280, 720, 1)
    r.event(3, .2, .28, r'{\fad(65,60)}sí')
    event = r.events[0]
    a, b = map(int, re.search(r'\\fad\((\d+),(\d+)\)', event).groups())
    assert a+b < 80


def test_resolution_scaling_does_not_rewrite_escaped_lyric_text():
    lyric = A._ass_escape(r'literal \fs40 and \pos(1,2)')
    result = _scale_text(r'{\pos(100,200)\fs40}' + lyric, 1.5)
    assert result.endswith(lyric)
