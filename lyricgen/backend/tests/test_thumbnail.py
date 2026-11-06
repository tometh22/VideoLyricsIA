"""The downloadable thumbnail must retain the selected intro's visual identity."""
import ast
import inspect
import os
import subprocess

import pytest
from PIL import Image, ImageFont

import ass_render as ass
import pipeline


FONT = os.path.join(pipeline._FONTS_DIR, "Oswald-Bold.ttf")
ARTIST_FONT = os.path.join(pipeline._FONTS_DIR, "Montserrat-ExtraBold.ttf")
CARD = dict(
    width=1280, height=720,
    lyric_font_family="Oswald", artist_font_family="Montserrat ExtraBold",
    lyric_font_path=FONT, artist_font_path=ARTIST_FONT,
)


def test_thumbnail_enlarges_intro_and_removes_fades():
    intro = ass.title_card_lines("Artista", "Canción", 5, text_scale=720 / 1080, **CARD)
    thumb = ass.thumbnail_title_card_lines("Artista", "Canción", 5, **CARD)
    for original, enlarged in zip(intro, thumb):
        assert enlarged.fontsize == pytest.approx(original.fontsize * 1.5, abs=1)
        assert enlarged.font_name == original.font_name
        assert enlarged.primary_alpha == original.primary_alpha
        assert enlarged.text == original.text
        assert enlarged.start_s == 0
        assert enlarged.end_s > 0
        assert enlarged.fade_in_ms == enlarged.fade_out_ms == 0


@pytest.mark.parametrize("template", ["centered", "lower_third", "badge", "auto"])
@pytest.mark.parametrize("intro", [0, 5])
def test_long_titles_and_manual_breaks_fit_the_frame(template, intro):
    lines = ass.thumbnail_title_card_lines(
        "Un artista con un nombre extremadamente largo", "Canción", intro,
        template=template, size_multiplier=2,
        song_lines=["Un título de canción muy largo", "UnaSegundaLíneaSinEspacios" * 5], **CARD,
    )
    assert "\n" in lines[-1].text
    layout = ass.title_card_layout(template, intro > 0.8)
    for line in lines:
        assert line.fontsize > 0
        font = ImageFont.truetype(ARTIST_FONT if line.bold else FONT, 100)
        for part in line.text.split("\n"):
            assert font.getlength(part) * line.fontsize / 100 <= 1280 * layout["card_w_frac"] + 1
        half_height = 1.2 * line.fontsize * len(line.text.split("\n")) / 720 / 2
        assert line.pos[1] - half_height >= 0.08 - 1e-6
        assert line.pos[1] + half_height <= 0.92 + 1e-6


def test_empty_artist_or_title_and_accents():
    assert ass.thumbnail_title_card_lines("", "", 0, **CARD) == []
    lines = ass.thumbnail_title_card_lines("", "Asi\u0301", 0, **CARD)
    assert len(lines) == 1
    assert lines[0].text == "Así"
    assert lines[0].fontsize > 1


@pytest.mark.parametrize("animation,color", [("none", "&H00332211"), ("karaoke", "&H00665544")])
def test_thumbnail_uses_selected_fonts_colors_and_compositor(tmp_path, monkeypatch, animation, color):
    bg = tmp_path / "bg.png"
    Image.new("RGB", (320, 180), (20, 30, 40)).save(bg)
    captured = {}

    def render(cmd, **kwargs):
        if kwargs["label"] == "thumbnail-title-card":
            captured["cmd"] = cmd
            captured["ass"] = (os.path.join(kwargs["cwd"], "thumbnail.ass"))
            with open(captured["ass"]) as f:
                captured["doc"] = f.read()
        Image.new("RGB", (1280, 720)).save(kwargs["output_path"])

    import fx_compositor
    real_filter = fx_compositor.build_video_filter

    def capture_filter(**kwargs):
        captured["filter"] = kwargs
        return real_filter(**kwargs)

    monkeypatch.setattr(pipeline, "run_checked", render)
    monkeypatch.setattr(fx_compositor, "build_video_filter", capture_filter)
    result = pipeline.generate_thumbnail(
        "Viejas Locas", "Me Gustas Mucho_Viejas Locas.mp3", str(tmp_path),
        bg_source=str(bg), font=FONT, style="oscuro", custom_colors="#123456",
        lyric_color="#112233", lyric_sung_color="#445566", lyrics_animation=animation,
        title_artist_font="anton", title_song_font="jost-bold", title_template="centered",
        title_song_break="Me Gustas\nMucho", text_contrast="strong",
    )
    doc = captured["doc"]
    assert "\\fnAnton" in doc and "\\fnJost" in doc
    assert "VIEJAS LOCAS" in doc and "Me Gustas\\NMucho" in doc
    assert color in doc
    assert "\\fad(" not in doc
    assert captured["filter"]["style"] == "oscuro"
    assert captured["filter"]["custom_colors"] == "#123456"
    assert captured["filter"]["fx_input_index"] == 1
    assert not os.path.exists(captured["filter"]["font_dir"])
    assert not os.path.exists(captured["ass"])
    assert Image.open(result).size == (1280, 720)


def test_thumbnail_cleans_up_if_render_fails(tmp_path, monkeypatch):
    bg = tmp_path / "bg.png"
    Image.new("RGB", (320, 180)).save(bg)
    captured = {}
    real_font_dir = ass.multi_font_dir

    def fonts(paths):
        captured["fonts"] = real_font_dir(paths)
        return captured["fonts"]

    def fail(*args, **kwargs):
        raise RuntimeError("encoder failed")

    monkeypatch.setattr(ass, "multi_font_dir", fonts)
    monkeypatch.setattr(pipeline, "run_checked", fail)
    with pytest.raises(RuntimeError, match="encoder failed"):
        pipeline.generate_thumbnail("Artista", "track.mp3", str(tmp_path), bg_source=str(bg), font=FONT)
    assert not os.path.exists(captured["fonts"])
    assert list(tmp_path.iterdir()) == [bg]


def test_generation_and_edits_forward_visual_settings():
    required = {
        "font", "style", "effect", "custom_colors", "text_contrast", "lyric_color",
        "lyric_sung_color", "lyrics_animation", "title_template", "title_size",
        "title_artist_font", "title_song_font", "title_song_break", "first_lyric_start",
        "lyric_look", "lyric_look_opts",
    }
    for fn in (pipeline.run_pipeline, pipeline.run_edit_pipeline):
        calls = [node for node in ast.walk(ast.parse(inspect.getsource(fn)))
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                 and node.func.id == "generate_thumbnail"]
        assert len(calls) == 1
        assert required <= {kw.arg for kw in calls[0].keywords}


def _has_libass():
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"],
                             capture_output=True, text=True, timeout=10)
        return " subtitles " in out.stdout
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.mark.skipif(not _has_libass(), reason="requires ffmpeg with libass")
@pytest.mark.parametrize("effect", ["", "dust", "rgb_glitch"])
def test_actual_thumbnail_render_has_colored_title(tmp_path, effect):
    bg = tmp_path / "background.png"
    Image.new("RGB", (800, 600), (20, 20, 20)).save(bg)
    result = pipeline.generate_thumbnail(
        "Viejas Locas", "track.mp3", str(tmp_path), bg_source=str(bg),
        song_title="Me Gustas Mucho", font=FONT, lyric_color="#a5ed00",
        title_artist_font="anton", style="oscuro", effect=effect,
    )
    with Image.open(result) as img:
        assert img.size == (1280, 720)
        # A visible green title on the very first frame proves the still has
        # no fade-in and that the style reaches the actual renderer.
        green = sum(g > 100 and g > r * 1.2 and g > b * 1.5 for r, g, b in img.convert("RGB").getdata())
        assert green > 1000


@pytest.mark.parametrize('look_id', __import__('lyric_looks').LOOKS)
def test_new_looks_keep_their_real_title_fonts_and_palette(tmp_path, monkeypatch, look_id):
    import lyric_look_runtime as runtime
    import lyric_looks as looks
    import fx_compositor
    opts = dict(genre='rock', variant=1, seed_key='viejas-locas',
                font_main='oswald', font_key='black-ops-one',
                ink='#FFFFFF', accent='#A5ED00')
    look = runtime.resolve_for_render(look_id, opts)
    captured = {}
    bg = tmp_path / 'bg.png'
    Image.new('RGB', (1280, 720), (15, 15, 15)).save(bg)
    monkeypatch.setattr(runtime, 'bg_luma', lambda path: None)

    def render(cmd, **kw):
        if kw['label'] == 'thumbnail-title-card':
            captured['doc'] = open(os.path.join(kw['cwd'], 'thumbnail.ass')).read()
        Image.new('RGB', (1280, 720)).save(kw['output_path'])

    def compositor(**kw):
        captured['filter'] = kw
        captured['fonts'] = os.listdir(kw['font_dir'])
        return 'null', False, []

    monkeypatch.setattr(pipeline, 'run_checked', render)
    monkeypatch.setattr(fx_compositor, 'build_video_filter', compositor)
    pipeline.generate_thumbnail('Viejas Locas', 'track.mp3', str(tmp_path),
                                bg_source=str(bg), song_title='Me Gustas Mucho',
                                lyric_look=look_id, lyric_look_opts=opts)
    doc = captured['doc']
    artist_family = ass.font_family(os.path.join(pipeline._FONTS_DIR, look.key_font or look.font_file))[0]
    song_family = ass.font_family(looks.font_path(look))[0]
    artist = next(row for row in doc.splitlines() if row.startswith('Dialogue:') and 'VIEJAS LOCAS' in row)
    song = next(row for row in doc.splitlines() if row.startswith('Dialogue:') and 'Me Gustas Mucho' in row)
    assert f'\\fn{artist_family}' in artist
    assert f'\\fn{song_family}' in song
    assert '\\b0' in artist  # no synthetic bold smearing decorative glyphs
    assert look.font_file in captured['fonts']
    assert captured['filter']['grade_override'] == look.grade
    assert captured['filter']['effect'] == look.effect
    assert 'Montserrat' not in artist
    if look.key_font:
        accent = look.line_colors[2] if len(look.line_colors) > 2 else look.accent
        if accent:
            assert ass.hex_to_ass(accent)[4:] in artist


@pytest.mark.skipif(not _has_libass(), reason='requires ffmpeg with libass')
def test_black_ops_artist_green_and_oswald_song_light(tmp_path):
    bg = tmp_path / 'bg.png'
    Image.new('RGB', (1280, 720), (20, 20, 20)).save(bg)
    result = pipeline.generate_thumbnail(
        'Viejas Locas', 'track.mp3', str(tmp_path), bg_source=str(bg),
        song_title='Me Gustas Mucho', lyric_look='cine',
        lyric_look_opts=dict(genre='rock', variant=1, seed_key='viejas-locas',
                             font_main='oswald', font_key='black-ops-one',
                             ink='#FFFFFF', accent='#A5ED00'),
    )
    with Image.open(result) as img:
        # Independent colors from the look, unlike the classic single-color path.
        upper = list(img.crop((0, 240, 1280, 365)).convert('RGB').getdata())
        lower = list(img.crop((0, 365, 1280, 500)).convert('RGB').getdata())
        assert sum(g > 130 and g > r * 1.2 and g > b * 1.5 for r, g, b in upper) > 1000
        assert sum(min(r, g, b) > 140 and max(r, g, b) - min(r, g, b) < 65 for r, g, b in lower) > 500


@pytest.mark.skipif(not _has_libass(), reason='requires ffmpeg with libass')
def test_chat_title_stays_visible_on_dark_background(tmp_path):
    bg = tmp_path / 'bg.png'
    Image.new('RGB', (1280, 720), (20, 20, 20)).save(bg)
    result = pipeline.generate_thumbnail(
        'Viejas Locas', 'track.mp3', str(tmp_path), bg_source=str(bg),
        song_title='Me Gustas Mucho', lyric_look='chat',
        lyric_look_opts=dict(variant=0, genre='', seed_key='viejas-locas'),
    )
    with Image.open(result) as img:
        pixels = img.crop((0, 250, 1280, 480)).convert('RGB').getdata()
        assert sum(min(rgb) > 160 for rgb in pixels) > 1000
