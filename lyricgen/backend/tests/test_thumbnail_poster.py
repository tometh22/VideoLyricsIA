"""Lyric-look thumbnails are a poster in the look's own typography."""
import os
import subprocess

import pytest
from PIL import Image

import lyric_look_runtime as runtime
import lyric_looks as looks
import pipeline
import thumbnail_poster as tp


OPTS = dict(variant=0, genre='', seed_key='rosa-de-fogo')
LONG_ARTIST = 'Los Fabulosos Cadillacs & Mercedes Sosa feat. Fito Páez'
LONG_TITLE = ('Matador (En Vivo Desde el Estadio Único de La Plata, '
              'Versión Extendida Remasterizada)')
LONG_WORD = 'Supercalifragilisticoespialidosoextraordinariamente'


def _safe_box():
    x0, y0, x1, y1 = tp.SAFE
    return x0 * tp.W, y0 * tp.H, x1 * tp.W, y1 * tp.H


def _fake_render(calls, color=(40, 40, 40)):
    """run_checked double: records labels and writes the expected image."""
    def render(cmd, **kw):
        calls.append(kw['label'])
        Image.new('RGB', (tp.W, tp.H) if 'poster' in kw['label'] or kw['label']
                  == 'thumbnail-background' else (1280, 720), color).save(
            kw['output_path'], 'PNG' if kw['output_path'].endswith('.png') else 'JPEG')
    return render


@pytest.mark.parametrize('look_id,layout', [
    ('cine', 'keyword'), ('bloque', 'block'), ('arco', 'arc'),
    ('romantico', 'script'), ('cuaderno', 'script'), ('diario', 'script'),
    ('golpe', 'stack'), ('neon', 'stack'), ('pop70', 'stack'),
])
def test_layout_follows_the_look_composition(look_id, layout):
    look = runtime.resolve_for_render(look_id, OPTS)
    assert tp.layout_for(look) == layout


@pytest.mark.parametrize('look_id', looks.LOOKS)
def test_poster_draws_with_the_effective_look_font(monkeypatch, look_id):
    look = runtime.resolve_for_render(look_id, OPTS)
    used = []
    real_font = tp._font

    def spy(path, size):
        used.append(os.path.basename(path))
        return real_font(path, size)

    monkeypatch.setattr(tp, '_font', spy)
    layer, st, _layout = tp.text_layer(look, artist='Diego Faria', title='Rosa de Fogo')
    assert layer.getchannel('A').getbbox() is not None
    assert os.path.basename(st.title_font) == look.font_file
    assert look.font_file in used
    assert 'Montserrat-ExtraBold.ttf' not in used or look.font_file == 'Montserrat-ExtraBold.ttf'


def test_recipe_variant_font_and_palette_reach_the_poster():
    """Variants (lyric_recipes) swap the face and colours: the thumbnail
    follows the resolved look, not the base one."""
    opts = dict(genre='rock', variant=1, seed_key='viejas-locas',
                font_main='oswald', font_key='black-ops-one',
                ink='#FFFFFF', accent='#A5ED00')
    look = runtime.resolve_for_render('cine', opts)
    st = tp.style_for(look)
    assert os.path.basename(st.title_font) == 'Oswald-Bold.ttf'
    assert os.path.basename(st.key_font) == 'BlackOpsOne-Regular.ttf'
    layer, _st, _ = tp.text_layer(look, artist='Viejas Locas', title='Me Gustas Mucho')
    greens = sum(1 for r, g, b, a in layer.getdata() if a > 200 and g > 180 and r < 200 and b < 80)
    # cine paints its key word in the ink; the accent belongs to the other looks.
    look_b = runtime.resolve_for_render('bloque', opts)
    layer_b, st_b, _ = tp.text_layer(look_b, artist='Viejas Locas', title='Me Gustas Mucho')
    assert st_b.accent == (0xA5, 0xED, 0x00)
    greens_b = sum(1 for r, g, b, a in layer_b.getdata() if a > 200 and g > 180 and r < 200 and b < 80)
    assert greens_b > 2000
    assert greens < greens_b


@pytest.mark.parametrize('look_id', looks.LOOKS)
@pytest.mark.parametrize('artist,title,lines', [
    (LONG_ARTIST, LONG_TITLE, None),
    ('Ñandú Ávila', LONG_WORD, None),
    (LONG_ARTIST, 'Matador', ['Matador del', LONG_WORD]),
    ('', 'Ölümüne Aşk', None),
])
def test_long_titles_never_leave_the_frame(look_id, artist, title, lines):
    look = runtime.resolve_for_render(look_id, OPTS)
    layer, _st, _ = tp.text_layer(look, artist=artist, title=title, song_lines=lines)
    bbox = layer.getchannel('A').getbbox()
    assert bbox is not None
    sx0, sy0, sx1, sy1 = _safe_box()
    assert bbox[0] >= sx0 - 1 and bbox[1] >= sy0 - 1
    assert bbox[2] <= sx1 + 1 and bbox[3] <= sy1 + 1


def test_parenthetical_becomes_a_subtitle():
    assert tp.split_title('Matador (En Vivo)') == ('Matador', 'En Vivo')
    assert tp.split_title('Despacito - Remix') == ('Despacito', 'Remix')
    assert tp.split_title('(Intro)') == ('(Intro)', '')
    assert tp.split_title('Rosa de Fogo') == ('Rosa de Fogo', '')
    look = runtime.resolve_for_render('cine', OPTS)
    _layer, st, _ = tp.text_layer(look, artist='A', title='Matador (En Vivo)')
    assert st.subtitle == 'EN VIVO'


def test_accents_and_cyrillic_render_with_real_glyphs():
    golpe = runtime.resolve_for_render('golpe', OPTS)  # Bungee: Latin only
    _layer, st, _ = tp.text_layer(golpe, artist='Кино', title='Группа крови')
    assert os.path.basename(st.title_font) == tp.FALLBACK_FONT
    cine = runtime.resolve_for_render('cine', OPTS)
    _layer, st, _ = tp.text_layer(cine, artist='Ñandú Ávila', title='Corazón Ç')
    assert os.path.basename(st.title_font) == cine.font_file
    assert tp.covers(st.title_font, 'CORAZÓN Ç ÑANDÚ')


def test_text_no_poster_font_can_draw_is_refused():
    look = runtime.resolve_for_render('cine', OPTS)
    with pytest.raises(tp.PosterUnsupported):
        tp.text_layer(look, artist='米津玄師', title='春')


def test_dark_ink_is_lifted_for_the_photo():
    chat = runtime.resolve_for_render('chat', OPTS)  # near-black message ink
    assert tp.style_for(chat).ink == (255, 255, 255)


def test_background_pass_drops_letterbox_and_keeps_painted_surfaces():
    cine = runtime.resolve_for_render('cine', OPTS)
    assert cine.letterbox
    assert 'Dialogue:' not in tp.background_ass(cine, tp.W, tp.H)
    pop70 = runtime.resolve_for_render('pop70', OPTS)
    doc = tp.background_ass(pop70, tp.W, tp.H)
    assert looks._c(pop70.flat_colors[0]) in doc


def test_generate_thumbnail_uses_the_poster_for_looks(tmp_path, monkeypatch):
    bg = tmp_path / 'bg.png'
    Image.new('RGB', (320, 180), (30, 30, 30)).save(bg)
    calls = []
    monkeypatch.setattr(pipeline, 'run_checked', _fake_render(calls))
    monkeypatch.setattr(runtime, 'bg_luma', lambda path: None)
    out = pipeline.generate_thumbnail(
        'Diego Faria', 'track.mp3', str(tmp_path), bg_source=str(bg),
        song_title='Rosa de Fogo', lyric_look='bloque', lyric_look_opts=OPTS)
    assert calls == ['thumbnail-background', 'thumbnail-poster-background']
    with Image.open(out) as img:
        assert img.format == 'JPEG'
        assert img.size == (1280, 720)
        # Bloque's coral accent row is on the left half of the frame.
        left = img.crop((0, 0, 640, 720)).convert('RGB').getdata()
        assert sum(1 for r, g, b in left if r > 200 and 70 < g < 140 and b < 120) > 500
    assert sorted(os.listdir(tmp_path)) == ['bg.png', 'thumbnail.jpg']


def test_no_look_keeps_the_title_card(tmp_path, monkeypatch):
    bg = tmp_path / 'bg.png'
    Image.new('RGB', (320, 180)).save(bg)
    calls = []
    monkeypatch.setattr(pipeline, 'run_checked', _fake_render(calls))
    monkeypatch.setattr(tp, 'compose', lambda *a, **k: pytest.fail('poster without a look'))
    pipeline.generate_thumbnail('Artista', 'track.mp3', str(tmp_path), bg_source=str(bg),
                                song_title='Canción',
                                font=os.path.join(pipeline._FONTS_DIR, 'Oswald-Bold.ttf'))
    assert calls == ['thumbnail-background', 'thumbnail-title-card']


@pytest.mark.parametrize('cause', ['kill_switch', 'poster_error', 'cjk'])
def test_poster_problems_fall_back_to_the_title_card(tmp_path, monkeypatch, cause):
    bg = tmp_path / 'bg.png'
    Image.new('RGB', (320, 180)).save(bg)
    calls = []
    monkeypatch.setattr(pipeline, 'run_checked', _fake_render(calls))
    monkeypatch.setattr(runtime, 'bg_luma', lambda path: None)
    artist, title = 'Diego Faria', 'Rosa de Fogo'
    if cause == 'kill_switch':
        monkeypatch.setenv('THUMBNAIL_LOOK_POSTER', '0')
    elif cause == 'poster_error':
        def boom(*a, **k):
            raise RuntimeError('pillow exploded')
        monkeypatch.setattr(tp, 'compose', boom)
    else:
        artist, title = '米津玄師', '春'
    out = pipeline.generate_thumbnail(artist, 'track.mp3', str(tmp_path), bg_source=str(bg),
                                      song_title=title, lyric_look='cine',
                                      lyric_look_opts=OPTS)
    assert calls[-1] == 'thumbnail-title-card'
    assert os.path.isfile(out)
    assert sorted(os.listdir(tmp_path)) == ['bg.png', 'thumbnail.jpg']


def test_missing_background_still_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, 'run_checked', _fake_render([]))
    with pytest.raises(FileNotFoundError):
        pipeline.generate_thumbnail('A', 'track.mp3', str(tmp_path),
                                    bg_source=str(tmp_path / 'nope.png'),
                                    song_title='B', lyric_look='cine', lyric_look_opts=OPTS)


def _has_libass():
    try:
        out = subprocess.run(['ffmpeg', '-hide_banner', '-filters'],
                             capture_output=True, text=True, timeout=10)
        return ' subtitles ' in out.stdout
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.mark.skipif(not _has_libass(), reason='requires ffmpeg with libass')
@pytest.mark.parametrize('look_id', ['cine', 'arco', 'pop70', 'romantico'])
def test_actual_poster_render(tmp_path, look_id):
    bg = tmp_path / 'background.png'
    Image.new('RGB', (800, 600), (90, 110, 130)).save(bg)
    out = pipeline.generate_thumbnail(
        'Diego Faria', 'track.mp3', str(tmp_path), bg_source=str(bg),
        song_title='Rosa de Fogo', lyric_look=look_id, lyric_look_opts=OPTS)
    with Image.open(out) as img:
        assert img.format == 'JPEG' and img.size == (1280, 720)
        bright = sum(1 for p in img.convert('L').getdata() if p > 215)
        assert bright > 2000
