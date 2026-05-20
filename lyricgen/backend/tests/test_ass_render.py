"""Unit tests for ass_render — the ASS subtitle generator for the fast
lyric-render path. Pure formatter, no moviepy/ImageMagick, so this runs
on any Python without the heavy render deps."""

from ass_render import AssLine, build_ass, _ass_time, _ass_escape


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
