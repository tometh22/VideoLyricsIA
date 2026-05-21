"""Unit tests for ass_render — the ASS subtitle generator for the fast
lyric-render path. Pure formatter, no moviepy/ImageMagick, so this runs
on any Python without the heavy render deps."""

import os

import pytest

from ass_render import (
    AssLine, build_ass, _ass_time, _ass_escape,
    lyric_fontsize, fade_seconds, perceptual_start,
    segments_to_lines, font_family, single_font_dir,
    multi_font_dir, title_card_lines, _opacity_to_alpha,
    moviepy_line_placement,
)

_FONTS_DIR = os.path.join(os.path.dirname(__file__), "..", "fonts")


def _font(name):
    return os.path.join(_FONTS_DIR, name)


def _have_pil():
    try:
        import PIL  # noqa: F401
        return True
    except Exception:
        return False


pil = pytest.mark.skipif(not _have_pil(), reason="Pillow not installed in this interpreter")


@pil
def test_font_family_resolves_name_and_weight_from_real_fonts():
    fam, bold = font_family(_font("Oswald-Bold.ttf"))
    assert "oswald" in fam.lower()
    assert bold is True
    fam2, bold2 = font_family(_font("Anton-Regular.ttf"))
    assert "anton" in fam2.lower()
    assert bold2 is False  # Regular display face — no synthetic bold


def test_single_font_dir_has_exactly_one_font():
    # Uses a font file if present; otherwise writes a dummy to test the
    # mechanics (copy into an isolated dir).
    src = _font("Oswald-Bold.ttf")
    if not os.path.isfile(src):
        pytest.skip("font asset not present")
    d = single_font_dir(src)
    try:
        files = os.listdir(d)
        assert files == ["Oswald-Bold.ttf"]
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def test_build_ass_bold_flag_toggles_style():
    out_bold = build_ass(width=1920, height=1080, font_name="Anton",
                         base_fontsize=85, outline=2, shadow=3, lines=[], bold=True)
    out_reg = build_ass(width=1920, height=1080, font_name="Anton",
                        base_fontsize=85, outline=2, shadow=3, lines=[], bold=False)
    # Style line: bold field is -1 (true) vs 0 (false).
    bold_style = [l for l in out_bold.splitlines() if l.startswith("Style: Lyric")][0]
    reg_style = [l for l in out_reg.splitlines() if l.startswith("Style: Lyric")][0]
    assert ",-1,0,0,0," in bold_style
    assert ",0,0,0,0," in reg_style


def test_segments_to_lines_parity_basics():
    segs = [
        {"start": 2.0, "end": 5.0, "text": "Hola mundo"},
        {"start": 6.0, "end": 6.05, "text": "   "},           # blank → skipped
        {"start": 10.0, "end": 14.0, "text": "línea con @ y `"},
    ]
    lines = segments_to_lines(segs, text_scale=1.0, lyric_transition="fade")
    assert len(lines) == 2  # blank skipped
    # @ stripped, ` → '
    assert lines[1].text == "línea con  y '"
    # fade=0.15s → 150ms, start shifted earlier by half (0.075s)
    assert lines[0].fade_in_ms == 150
    assert abs(lines[0].start_s - (2.0 - 0.075)) < 1e-9
    assert lines[0].end_s == 5.0


def test_segments_to_lines_applies_case_fn_then_sizes_by_length():
    segs = [{"start": 0.0, "end": 3.0, "text": "abc"}]
    lines = segments_to_lines(
        segs, text_scale=1.0, case_fn=str.upper, lyric_transition="cut",
    )
    assert lines[0].text == "ABC"
    assert lines[0].fontsize == 85   # short line tier
    assert lines[0].fade_in_ms == 0  # cut → no fade


def test_lyric_fontsize_tiers_mirror_legacy():
    # scale=1.0 → tiers 85/70/55, floored at 18.
    assert lyric_fontsize(10, 1.0) == 85
    assert lyric_fontsize(60, 1.0) == 70   # >50
    assert lyric_fontsize(90, 1.0) == 55   # >80
    # font_scale clamps to [0.6, 1.5].
    assert lyric_fontsize(10, 1.0, font_scale=2.0) == int(round(85 * 1.5))
    assert lyric_fontsize(10, 1.0, font_scale=0.1) == max(18, int(round(85 * 0.6)))
    # scale applies before font_scale.
    assert lyric_fontsize(10, 2.0) == 170


def test_fade_seconds_and_perceptual_offset():
    assert fade_seconds("cut", 10) == 0.0
    assert fade_seconds("fade", 10) == 0.15
    assert fade_seconds("fade_slow", 10) == 0.30
    # capped at seg/3 for short lines (float, compare with tolerance)
    assert abs(fade_seconds("fade_slow", 0.6) - 0.2) < 1e-9
    # perceptual start shifts earlier by half the fade, clamped at 0
    assert perceptual_start(5.0, 0.30) == 4.85
    assert perceptual_start(0.05, 0.30) == 0.0


def test_ass_time_formats_centiseconds():
    assert _ass_time(0) == "0:00:00.00"
    assert _ass_time(5.958) == "0:00:05.96"  # rounds to cs
    assert _ass_time(65.5) == "0:01:05.50"
    assert _ass_time(3661.25) == "1:01:01.25"
    assert _ass_time(-3) == "0:00:00.00"  # clamps negatives


def test_ass_escape_neutralises_braces_and_newlines():
    assert _ass_escape("hola {mundo}") == "hola (mundo)"
    assert _ass_escape("línea1\nlínea2") == "línea1\\Nlínea2"
    assert _ass_escape("a\r\nb") == "a\\Nb"


def test_build_ass_has_required_sections_and_playres():
    out = build_ass(
        width=1920, height=1080, font_name="Montserrat-Bold",
        base_fontsize=85, outline=2, shadow=3, lines=[],
    )
    assert "[Script Info]" in out
    assert "PlayResX: 1920" in out
    assert "PlayResY: 1080" in out
    assert "[V4+ Styles]" in out
    assert "[Events]" in out
    # White primary, black outline encoded as ABGR.
    assert "&H00FFFFFF" in out  # white fill
    assert "Montserrat-Bold" in out


def test_build_ass_emits_one_dialogue_per_line_with_size_and_fade():
    lines = [
        AssLine(text="primera línea", start_s=2.33, end_s=5.96,
                fontsize=85, fade_in_ms=150, fade_out_ms=150),
        AssLine(text="segunda", start_s=6.0, end_s=8.0, fontsize=70),
    ]
    out = build_ass(
        width=1920, height=1080, font_name="Oswald",
        base_fontsize=85, outline=2, shadow=3, lines=lines,
    )
    dialogues = [l for l in out.splitlines() if l.startswith("Dialogue:")]
    assert len(dialogues) == 2
    # First line: timing, per-line size override, fade.
    assert "0:00:02.33" in dialogues[0]
    assert "0:00:05.96" in dialogues[0]
    assert "\\fs85" in dialogues[0]
    assert "\\fad(150,150)" in dialogues[0]
    assert "primera línea" in dialogues[0]
    # Second line: no fade tag (both zero), different size.
    assert "\\fs70" in dialogues[1]
    assert "\\fad(" not in dialogues[1]


def test_build_ass_skips_empty_and_inverted_lines():
    lines = [
        AssLine(text="   ", start_s=1.0, end_s=2.0, fontsize=85),       # blank
        AssLine(text="ok", start_s=5.0, end_s=4.0, fontsize=85),         # end<=start
        AssLine(text="válida", start_s=10.0, end_s=12.0, fontsize=85),   # good
    ]
    out = build_ass(
        width=1080, height=1920, font_name="Arial",
        base_fontsize=85, outline=2, shadow=3, lines=lines,
    )
    dialogues = [l for l in out.splitlines() if l.startswith("Dialogue:")]
    assert len(dialogues) == 1
    assert "válida" in dialogues[0]


def test_build_ass_emits_pos_and_rotation_overrides():
    lines = [
        AssLine(text="torcida", start_s=1.0, end_s=3.0, fontsize=85,
                pos=(0.25, 0.75), rot=-8),
    ]
    out = build_ass(width=1920, height=1080, font_name="Anton",
                    base_fontsize=85, outline=2, shadow=3, lines=lines)
    d = [l for l in out.splitlines() if l.startswith("Dialogue:")][0]
    # pos fraction → pixels, anchored center (\an5)
    assert "\\an5\\pos(480,810)" in d   # 0.25*1920=480, 0.75*1080=810
    # CSS clockwise -8 → ASS counter-clockwise +8
    assert "\\frz8" in d


def test_build_ass_no_layout_override_when_absent():
    lines = [AssLine(text="centrada", start_s=0.0, end_s=2.0, fontsize=85)]
    out = build_ass(width=1920, height=1080, font_name="Anton",
                    base_fontsize=85, outline=2, shadow=3, lines=lines)
    d = [l for l in out.splitlines() if l.startswith("Dialogue:")][0]
    assert "\\pos(" not in d and "\\frz" not in d  # uses style default (centered)


def test_segments_to_lines_reads_pos_scale_rot():
    segs = [{
        "start": 1.0, "end": 4.0, "text": "abc",
        "pos": {"x": 0.3, "y": 0.6}, "scale": 1.5, "rot": -10,
    }]
    lines = segments_to_lines(segs, text_scale=1.0, lyric_transition="cut")
    ln = lines[0]
    assert ln.pos == (0.3, 0.6)
    assert ln.rot == -10
    assert ln.fontsize == int(round(85 * 1.5))  # short-line tier 85 × scale


def test_build_ass_text_with_braces_does_not_break_override_block():
    lines = [AssLine(text="texto {raro}", start_s=0.0, end_s=2.0, fontsize=85)]
    out = build_ass(
        width=1920, height=1080, font_name="Arial",
        base_fontsize=85, outline=2, shadow=3, lines=lines,
    )
    dialogue = [l for l in out.splitlines() if l.startswith("Dialogue:")][0]
    # The override block we control must be intact; user braces neutralised.
    assert dialogue.count("{") == 1 and dialogue.count("}") == 1
    assert "(raro)" in dialogue


# --- Title card + per-line style overrides --------------------------------

def test_multi_font_dir_copies_and_dedupes(tmp_path):
    # Two distinct fonts → both copied; same path twice → copied once.
    a = tmp_path / "FontA.ttf"; a.write_bytes(b"A")
    b = tmp_path / "FontB.ttf"; b.write_bytes(b"B")
    d = multi_font_dir([str(a), str(b), str(a), "", "/nope/missing.ttf"])
    try:
        assert sorted(os.listdir(d)) == ["FontA.ttf", "FontB.ttf"]
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def test_opacity_to_alpha_inverts_and_clamps():
    assert _opacity_to_alpha(1.0) == 0      # opaque
    assert _opacity_to_alpha(0.0) == 255    # transparent
    assert _opacity_to_alpha(0.97) == 8     # artist base opacity
    assert _opacity_to_alpha(0.85) == 38    # song base opacity
    assert _opacity_to_alpha(2.0) == 0      # clamp


def test_title_card_long_intro_centred_card():
    lines = title_card_lines(
        "Soda Stereo", "De Música Ligera", first_lyric_start=5.0,
        width=1920, height=1080, text_scale=1.0,
        lyric_font_family="Oswald", artist_font_family="Montserrat ExtraBold",
    )
    assert len(lines) == 2
    artist, song = lines
    # Centred layout (\an5), artist in ExtraBold over the song.
    assert artist.alignment == 5 and song.alignment == 5
    assert artist.text == "SODA STEREO" and artist.bold is True
    assert artist.font_name == "Montserrat ExtraBold"
    assert song.text == "De Música Ligera" and song.bold is False
    assert song.font_name == "Oswald"
    # Sizes mirror moviepy tiers at scale 1.0.
    assert artist.fontsize == 62 and song.fontsize == 46
    # Visible 0.3s → min(first_lyric-0.2, 8.3) = 4.8s.
    assert artist.start_s == 0.3 and abs(artist.end_s - 4.8) < 1e-9
    # Per-line opacity mapped to \1a alpha.
    assert artist.primary_alpha == 8 and song.primary_alpha == 38
    # Centred horizontally.
    assert artist.pos[0] == 0.5


def test_title_card_short_intro_lower_left_badge():
    lines = title_card_lines(
        "Intoxicados", "No Tengo Ganas", first_lyric_start=0.2,
        width=1920, height=1080, text_scale=1.0,
        lyric_font_family="Anton", artist_font_family="Montserrat ExtraBold",
    )
    assert len(lines) == 2
    artist, song = lines
    # Left-anchored badge (\an4), smaller sizes, visible 0.3 → 6.3s.
    assert artist.alignment == 4 and song.alignment == 4
    assert artist.fontsize == 36 and song.fontsize == 28
    assert abs(artist.end_s - 6.3) < 1e-9
    # 6% left margin (fraction), not centred.
    assert abs(artist.pos[0] - 0.06) < 1e-9


def test_title_card_empty_returns_nothing():
    assert title_card_lines("", "", 5.0, width=1920, height=1080,
                            text_scale=1.0, lyric_font_family="X",
                            artist_font_family="Y") == []


def test_build_ass_emits_title_card_style_overrides():
    ln = AssLine(
        text="SODA STEREO", start_s=0.3, end_s=4.8, fontsize=62,
        fade_in_ms=400, fade_out_ms=700, pos=(0.5, 0.45),
        font_name="Montserrat ExtraBold", bold=True, alignment=5,
        primary_alpha=8,
    )
    out = build_ass(width=1920, height=1080, font_name="Oswald",
                    base_fontsize=85, outline=2, shadow=3, lines=[ln])
    d = [l for l in out.splitlines() if l.startswith("Dialogue:")][0]
    assert "\\fnMontserrat ExtraBold" in d
    assert "\\b1" in d
    assert "\\an5\\pos(960,486)" in d   # 0.5*1920, 0.45*1080
    assert "\\1a&H08&" in d
    assert "\\fad(400,700)" in d


def test_build_ass_alignment_without_pos():
    ln = AssLine(text="badge", start_s=0.3, end_s=6.3, fontsize=36,
                 alignment=4, bold=False)
    out = build_ass(width=1920, height=1080, font_name="Anton",
                    base_fontsize=85, outline=2, shadow=3, lines=[ln])
    d = [l for l in out.splitlines() if l.startswith("Dialogue:")][0]
    assert "\\an4" in d and "\\pos(" not in d
    assert "\\b0" in d


# --- moviepy layout parity (the math behind _make_text_clip's per-line
# pos override; the moviepy clip.rotate stays in pipeline, untestable here) ---

def test_moviepy_placement_none_is_frame_center():
    # No override → clip centered in the frame (legacy behavior).
    x, y = moviepy_line_placement(None, clip_w=600, clip_h=100,
                                  frame_w=1920, frame_h=1080)
    assert (x + 600 / 2, y + 100 / 2) == (960, 540)


def test_moviepy_placement_centers_on_fraction():
    # pos is the line CENTER as 0..1 fractions (same mapping build_ass uses).
    x, y = moviepy_line_placement((0.25, 0.75), clip_w=600, clip_h=100,
                                  frame_w=1920, frame_h=1080)
    assert x + 600 / 2 == 0.25 * 1920   # center_x = 480
    assert y + 100 / 2 == 0.75 * 1080   # center_y = 810


def test_moviepy_placement_applies_screen_offset():
    base = moviepy_line_placement((0.5, 0.5), 600, 100, 1920, 1080)
    off = moviepy_line_placement((0.5, 0.5), 600, 100, 1920, 1080, dx=3, dy=3)
    assert off == (base[0] + 3, base[1] + 3)
