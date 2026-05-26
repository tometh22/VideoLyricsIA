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
    moviepy_line_placement, hex_to_ass,
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
    # Hero sizes mirror moviepy tiers at scale 1.0 (artist > 85px lyric tier).
    assert artist.fontsize == 100 and song.fontsize == 62
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


# ---------------------------------------------------------------------------
# Lyric-animation templates (libass tags, fast path). All emit inside the
# single ffmpeg pass — these assert the right tags per template.
# ---------------------------------------------------------------------------

from ass_render import _word_timings, _animated_word_payload  # noqa: E402


def _dialogue(out):
    return [l for l in out.splitlines() if l.startswith("Dialogue:")][0]


def _ass(lines):
    return build_ass(width=1920, height=1080, font_name="Oswald",
                     base_fontsize=85, outline=2, shadow=3, lines=lines)


def test_segments_to_lines_threads_animation_line_level():
    segs = [{"text": "hola mundo", "start": 1.0, "end": 3.0}]
    lines = segments_to_lines(segs, text_scale=1.0, animation="pop")
    assert lines[0].animation == "pop"
    assert lines[0].words is None  # line-level needs no word data


def test_segments_to_lines_synthesizes_words_when_absent():
    # The common production case: segments_json has no per-word array.
    segs = [{"text": "uno dos tres", "start": 2.0, "end": 5.0}]
    lines = segments_to_lines(segs, text_scale=1.0, animation="karaoke")
    w = lines[0].words
    assert w is not None and len(w) == 3
    assert w[0]["start"] == pytest.approx(2.0)        # spans the line window
    assert w[-1]["end"] == pytest.approx(5.0)
    # monotonic, non-overlapping
    assert w[0]["end"] <= w[1]["start"] <= w[1]["end"] <= w[2]["start"]


def test_word_timings_uses_real_words_when_consistent():
    raw = [{"word": "hola", "start": 1.0, "end": 1.4},
           {"word": "mundo", "start": 1.5, "end": 2.0}]
    w = _word_timings("hola mundo", 1.0, 2.0, raw)
    assert [x["word"] for x in w] == ["hola", "mundo"]
    assert w[0]["start"] == pytest.approx(1.0)
    assert w[1]["end"] == pytest.approx(2.0)


def test_word_timings_rejects_stale_words_and_synthesizes():
    # Operator edited the line after Whisper → words don't match the text.
    raw = [{"word": "viejo", "start": 1.0, "end": 1.4},
           {"word": "texto", "start": 1.5, "end": 2.0}]
    w = _word_timings("nuevo contenido", 1.0, 2.0, raw)
    # Falls back to synthesis over the NEW tokens, not the stale words.
    assert [x["word"] for x in w] == ["nuevo", "contenido"]


def test_build_ass_pop_emits_scale_overshoot():
    ln = AssLine(text="hola", start_s=1.0, end_s=3.0, fontsize=85,
                 animation="pop")
    d = _dialogue(_ass([ln]))
    assert "\\fscx116\\fscy116" in d
    assert d.count("\\t(") == 2          # overshoot + settle
    assert "\\fscx100\\fscy100" in d


def test_build_ass_glow_emits_blur_pingpong_outline_never_drops():
    ln = AssLine(text="hola", start_s=1.0, end_s=3.0, fontsize=85,
                 animation="glow")
    d = _dialogue(_ass([ln]))
    assert "\\blur2" in d and "\\blur6" in d
    assert d.count("\\t(") == 2          # breathe in + out
    # base outline (2) is the floor; the boost is 3.5, never below 2
    assert "\\bord2" in d and "\\bord3.5" in d


def test_build_ass_word_reveal_per_word_alpha_and_balanced_braces():
    segs = [{"text": "uno dos tres", "start": 1.0, "end": 4.0}]
    # The per-word reveal IS the entrance AND exit (words appear and leave one
    # by one), so the line carries no \fad and each word has an enter + exit \t.
    ln = segments_to_lines(segs, text_scale=1.0, animation="word_reveal",
                           lyric_transition="fade")[0]
    d = _dialogue(_ass([ln]))
    assert d.count("\\alpha&H00&") == 3          # one reveal target per word
    assert d.count("\\t(") == 6                    # enter + exit per word
    # our control braces stay balanced (prefix block + 3 word blocks)
    assert d.count("{") == d.count("}")
    assert "\\fad(" not in d                       # per-word alpha owns enter+exit


def test_build_ass_karaoke_emits_kf_and_color_split():
    segs = [{"text": "uno dos", "start": 1.0, "end": 2.0}]
    ln = segments_to_lines(segs, text_scale=1.0, animation="karaoke")[0]
    d = _dialogue(_ass([ln]))
    assert "\\2c&H00808080&" in d and "\\1c&H00FFFFFF&" in d  # unsung/sung
    assert d.count("\\kf") == 2                   # one fill chunk per word
    assert d.count("{") == d.count("}")


def test_build_ass_word_anim_escapes_user_braces():
    # User text with braces must be neutralized; our override braces survive.
    segs = [{"text": "a {x} b", "start": 1.0, "end": 3.0}]
    ln = segments_to_lines(segs, text_scale=1.0, animation="word_reveal")[0]
    d = _dialogue(_ass([ln]))
    assert "(x)" in d                             # user braces → parens
    assert d.count("{") == d.count("}")           # still balanced


def test_build_ass_none_animation_has_no_anim_tags():
    # Regression: animation="none" must match the legacy output (no extra tags).
    ln = AssLine(text="hola mundo", start_s=1.0, end_s=3.0, fontsize=85,
                 animation="none", fade_in_ms=150, fade_out_ms=150)
    d = _dialogue(_ass([ln]))
    assert "\\t(" not in d and "\\kf" not in d
    assert "\\fscx" not in d and "\\blur" not in d
    assert d.endswith("hola mundo")
    assert "\\fad(150,150)" in d


# ---------------------------------------------------------------------------
# Line-to-line MOTION transitions (orthogonal to animation, compose with it).
# ---------------------------------------------------------------------------

def _line(segs, **kw):
    return segments_to_lines(segs, text_scale=1.0, **kw)[0]


def test_build_ass_slide_up_emits_move_from_below():
    segs = [{"text": "hola", "start": 1.0, "end": 5.0}]
    d = _dialogue(_ass([_line(segs, transition="slide_up")]))
    # center is (960,540) on 1920x1080; enters from below (py + offset)
    assert "\\move(960,616,960,540,0," in d
    assert "\\an5" in d


def test_build_ass_slide_side_emits_horizontal_move():
    segs = [{"text": "hola", "start": 1.0, "end": 5.0}]
    d = _dialogue(_ass([_line(segs, transition="slide_side")]))
    assert "\\move(" in d and ",540,960,540,0," in d   # same y, enters from left


def test_build_ass_wipe_emits_animated_clip():
    segs = [{"text": "hola", "start": 1.0, "end": 5.0}]
    d = _dialogue(_ass([_line(segs, transition="wipe")]))
    assert "\\clip(0,0,0,1080)" in d
    assert "\\t(0," in d and "\\clip(0,0,1920,1080))" in d


def test_build_ass_dissolve_blur_emits_blur_in_and_out():
    segs = [{"text": "hola", "start": 1.0, "end": 5.0}]
    d = _dialogue(_ass([_line(segs, transition="dissolve_blur")]))
    assert "\\blur8" in d and "\\blur0" in d
    assert d.count("\\t(") == 2          # focus-in + blur-out


def test_word_reveal_adds_staggered_exit():
    # Long enough line → words also fade OUT one by one (per-word exit \t).
    segs = [{"text": "uno dos tres", "start": 1.0, "end": 6.0}]
    d = _dialogue(_ass([_line(segs, animation="word_reveal")]))
    # each word: enter \t(...\alpha&H00&) + exit \t(...\alpha&HFF&) = 2 per word
    assert d.count("\\t(") == 6
    assert d.count("\\alpha&H00&") == 3 and d.count("\\alpha&HFF&") == 6
    # line carries NO \fad (per-word alpha owns enter+exit)
    assert "\\fad(" not in d


def test_transition_composes_with_animation():
    # karaoke (colour/kf) + slide_up (\move) must both appear, no clash.
    segs = [{"text": "uno dos", "start": 1.0, "end": 4.0}]
    d = _dialogue(_ass([_line(segs, animation="karaoke", transition="slide_up")]))
    assert "\\move(" in d              # transition
    assert "\\kf" in d                 # animation
    assert "\\2c&H00808080&" in d
    assert d.count("{") == d.count("}")


def test_transition_none_emits_no_motion_tags():
    segs = [{"text": "hola", "start": 1.0, "end": 5.0}]
    d = _dialogue(_ass([_line(segs, transition="none")]))
    assert "\\move(" not in d and "\\clip(" not in d and "\\blur" not in d


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


# ─── Lyric text colors (PR 2026-05-25) ──────────────────────────────────

def test_hex_to_ass_red():
    """Red #FF0000 → &H000000FF (alpha=00, B=00, G=00, R=FF)."""
    assert hex_to_ass("#FF0000") == "&H000000FF"


def test_hex_to_ass_karaoke_green():
    """Karaoke green #19E0BC → &H00BCE019 (alpha=00, B=BC, G=E0, R=19)."""
    assert hex_to_ass("#19E0BC") == "&H00BCE019"


def test_hex_to_ass_white_default():
    """White #FFFFFF → &H00FFFFFF (preserva el default del style line)."""
    assert hex_to_ass("#FFFFFF") == "&H00FFFFFF"


def test_hex_to_ass_lowercase_hex_works():
    """Hex con letras minúsculas se acepta y se normaliza a mayúsculas."""
    assert hex_to_ass("#ff00cc") == "&H00CC00FF"


def test_hex_to_ass_empty_falls_back():
    """Empty string → fallback (default white) sin crashear."""
    assert hex_to_ass("") == "&H00FFFFFF"
    # Fallback customizable.
    assert hex_to_ass("", fallback="&H00808080") == "&H00808080"


def test_hex_to_ass_malformed_falls_back():
    """Strings malformados nunca llegan a libass."""
    for bad in ("nope", "#FFF", "#GGGGGG", "FF0000", "#fffffff", None, 123):
        assert hex_to_ass(bad) == "&H00FFFFFF"


def test_build_ass_uses_custom_primary_color():
    """Cuando primary_color se setea, el PrimaryColour del style refleja
    el hex. Bug original: build_ass hardcodeaba &H00FFFFFF (blanco)."""
    out = build_ass(
        width=1920, height=1080, font_name="Arial",
        base_fontsize=40, outline=2.0, shadow=2,
        lines=[], primary_color="#FF0000",
    )
    # Style line: "Style: Lyric,Arial,40,&H000000FF,..."
    assert "&H000000FF" in out
    # El default blanco NO debería estar en el slot de primary.
    style_line = [l for l in out.splitlines() if l.startswith("Style: Lyric,")][0]
    fields = style_line.split(",")
    # Format: Name,Font,Size,Primary,Secondary,Outline,Back,...
    assert fields[3] == "&H000000FF"


def test_build_ass_secondary_color_for_karaoke():
    """SecondaryColour respeta lyric_color (un-sung) cuando lo seteamos."""
    out = build_ass(
        width=1920, height=1080, font_name="Arial",
        base_fontsize=40, outline=2.0, shadow=2,
        lines=[], primary_color="#00FF00", secondary_color="#808080",
    )
    style_line = [l for l in out.splitlines() if l.startswith("Style: Lyric,")][0]
    fields = style_line.split(",")
    assert fields[3] == "&H0000FF00"  # primary = green
    assert fields[4] == "&H00808080"  # secondary = grey


def test_build_ass_defaults_to_white_when_no_colors_given():
    """Backwards compat: jobs sin colores siguen rindiendo blanco (default
    histórico del PrimaryColour libass)."""
    out = build_ass(
        width=1920, height=1080, font_name="Arial",
        base_fontsize=40, outline=2.0, shadow=2,
        lines=[],
    )
    style_line = [l for l in out.splitlines() if l.startswith("Style: Lyric,")][0]
    fields = style_line.split(",")
    assert fields[3] == "&H00FFFFFF"  # primary white
    assert fields[4] == "&H000000FF"  # secondary default (red, was hardcoded)


def test_build_ass_karaoke_override_uses_custom_colors():
    """El override per-line `\\2c\\1c` que build_ass mete en cada Dialogue
    karaoke usa los colores del operador, no los hardcoded grey/white."""
    segments = [{"start": 0.0, "end": 2.0, "text": "hola mundo"}]
    lines = segments_to_lines(segments, text_scale=1.0, animation="karaoke")
    out = build_ass(
        width=1920, height=1080, font_name="Arial",
        base_fontsize=40, outline=2.0, shadow=2,
        lines=lines,
        primary_color="#00FF00",   # sung = green
        secondary_color="#FF00FF", # un-sung = magenta
    )
    dialogue = [l for l in out.splitlines() if l.startswith("Dialogue:")][0]
    # ASS BGR: #FF00FF (magenta) → &H00FF00FF; #00FF00 (green) → &H0000FF00.
    assert "\\2c&H00FF00FF" in dialogue
    assert "\\1c&H0000FF00" in dialogue


def test_build_ass_karaoke_falls_back_to_grey_white_when_no_colors():
    """Sin colores custom, el override mantiene el look histórico
    (un-sung grey + sung white) — backwards compat con jobs viejos."""
    segments = [{"start": 0.0, "end": 2.0, "text": "hola mundo"}]
    lines = segments_to_lines(segments, text_scale=1.0, animation="karaoke")
    out = build_ass(
        width=1920, height=1080, font_name="Arial",
        base_fontsize=40, outline=2.0, shadow=2,
        lines=lines,
    )
    dialogue = [l for l in out.splitlines() if l.startswith("Dialogue:")][0]
    assert "\\2c&H00808080" in dialogue   # grey un-sung default
    assert "\\1c&H00FFFFFF" in dialogue   # white sung default

