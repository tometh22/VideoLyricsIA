"""Unit tests for ass_render — the ASS subtitle generator for the fast
lyric-render path. Pure formatter, no moviepy/ImageMagick, so this runs
on any Python without the heavy render deps."""

import os

import pytest

from ass_render import (
    AssLine, build_ass, _ass_time, _ass_escape,
    lyric_fontsize, fade_seconds, perceptual_start,
    segments_to_lines, font_family, single_font_dir,
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
