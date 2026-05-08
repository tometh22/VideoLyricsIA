"""Unit tests for `_fix_lrc_first_line_at_zero`.

Covers the lrclib "first line at [00:00.00]" quirk where a community-
curated LRC anchors line 1 to song time 0 even when the song has a
long instrumental intro before vocals start. The fixer detects this
via cadence (large gap between line 1 and line 2 vs. median gap of
subsequent lines) and relocates line 1 to roughly where a normal
verse cadence would put it.
"""

from pipeline import _fix_lrc_first_line_at_zero


def _seg(start, end, text="x"):
    return {"start": float(start), "end": float(end), "text": text}


def test_intoxicados_quirk_shifts_line_one():
    """Real-world repro: Intoxicados — "No Tengo Ganas". lrclib has
    line 1 at 0s, line 2 at 23.5s, and subsequent ~8 s gaps. Line 1
    should be shifted to roughly the median-gap window before line 2
    (~15.5 s)."""
    segs = [
        _seg(0.0,  5.0,  "No tengo ganas de seguir, pero tampoco tengo ganas de parar."),
        _seg(23.5, 30.0, "Tendría que pensar qué me está pasando..."),
        _seg(31.4, 38.0, "Podría quedarme durmiendo..."),
        _seg(39.2, 46.0, "Podría dejarle mi destino..."),
        _seg(47.3, 54.0, "Tengo apostando todo..."),
        _seg(55.2, 62.0, "Voy a tener que dejar..."),
        _seg(62.6, 70.0, "Es que tengo que dejar..."),
    ]
    out, moved = _fix_lrc_first_line_at_zero(segs, audio_duration=311.0)
    assert moved is not None
    assert 14.0 < moved < 17.0, f"expected ~15.5 s, got {moved}"
    assert out[1]["start"] == 23.5, "line 2 must stay put"
    assert out[0]["end"] <= out[1]["start"] - 0.05, "line 1 must not bleed into line 2"


def test_normal_song_no_intro_is_left_alone():
    """A song that genuinely starts at t=0 (line 1 cadence matches the
    rest) should not be mutated — the gap ratio fails the >2× check."""
    segs = [_seg(i * 4.0, i * 4.0 + 4.0) for i in range(6)]
    out, moved = _fix_lrc_first_line_at_zero(segs)
    assert moved is None
    assert out is segs or out == segs


def test_genuine_intro_above_one_second_is_left_alone():
    """When the LRC already places line 1 past 1 s the heuristic
    bails — the intro-trim path in the editor handles those."""
    segs = [_seg(5.0, 10.0)] + [_seg(18.0 + i * 8, 26.0 + i * 8) for i in range(5)]
    out, moved = _fix_lrc_first_line_at_zero(segs)
    assert moved is None


def test_too_few_segments_is_a_noop():
    """Need at least 4 segments to estimate a median gap reliably."""
    out, moved = _fix_lrc_first_line_at_zero([_seg(0, 5)])
    assert moved is None
    assert len(out) == 1


def test_first_line_clamps_against_audio_duration():
    """Edge: a tiny audio_duration that's smaller than the suggested
    new_end should clamp the segment so we never return end > duration."""
    segs = [
        _seg(0.0, 30.0, "first"),
        _seg(40.0, 48.0, "second"),
        _seg(48.0, 56.0, "third"),
        _seg(56.0, 64.0, "fourth"),
        _seg(64.0, 72.0, "fifth"),
    ]
    out, moved = _fix_lrc_first_line_at_zero(segs, audio_duration=42.0)
    assert moved is not None
    assert out[0]["end"] <= 42.0
    assert out[0]["end"] <= out[1]["start"] - 0.05
