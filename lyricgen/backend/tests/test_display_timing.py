"""Tests for _apply_display_timing — the lyric on-screen window logic.

Pins the fix for UMG "Costumbres argentinas" 2026-05-19: long lines whose
text disappeared before the vocal finished. The fix holds each line until
the next one starts (capped), instead of cutting at the transcribed end.
"""

import pipeline

GAP = 0.05
HOLD = 4.0


def _seg(start, end, text="x"):
    return {"start": start, "end": end, "text": text}


def test_short_line_extends_to_cover_vocal_when_gap_follows():
    """The core bug: a line transcribed as ending early, with a gap before
    the next line, must HOLD so it covers the full sung phrase."""
    segs = [_seg(10.0, 11.0), _seg(13.0, 14.0)]  # 2s gap after line 1
    out = pipeline._apply_display_timing(segs, duration=60.0)
    # Line 1 held: extended past its 11.0 end toward next.start (13.0),
    # capped at +HOLD. 11+4=15 > 13-0.05 → ceiling wins → 12.95.
    assert out[0]["end"] == 13.0 - GAP, out[0]
    assert out[0]["end"] > 11.0, "line must hold past its transcribed end"


def test_long_instrumental_gap_is_capped_not_lingering():
    """If the gap after a line is huge (instrumental), the line must NOT
    linger the whole break — capped at base_end + HOLD."""
    segs = [_seg(10.0, 11.0), _seg(40.0, 41.0)]  # 29s gap
    out = pipeline._apply_display_timing(segs, duration=60.0)
    assert out[0]["end"] == 11.0 + HOLD, out[0]  # 15.0, not ~39.95


def test_no_two_subtitles_overlap():
    """Every line must end strictly before the next begins (>= GAP)."""
    segs = [_seg(0.0, 5.0), _seg(4.0, 8.0), _seg(7.5, 10.0)]  # overlapping
    out = pipeline._apply_display_timing(segs, duration=30.0)
    for a, b in zip(out, out[1:]):
        assert a["end"] <= b["start"], f"{a} overlaps {b}"


def test_no_overlap_wins_over_minimum_readable_duration():
    """Regression: applying the 300ms floor after the neighbour ceiling used
    to create an overlap that was absent from the source timeline."""
    segs = [_seg(1.0, 1.1), _seg(1.2, 2.0)]
    out = pipeline._apply_display_timing(segs, duration=3.0)
    assert out[0]["end"] == 1.2 - GAP
    assert out[0]["end"] < 1.0 + 0.3
    assert out[0]["end"] < out[1]["start"]


def test_approved_timeline_is_rendered_exactly_without_display_transform():
    approved = [
        _seg(1.0, 1.1, "short approved line"),
        _seg(1.2, 2.0, "next approved line"),
    ]
    out = pipeline._effective_render_segments(
        approved, duration=3.0, preserve_approved_timing=True,
    )
    assert out == approved
    assert out is not approved


def test_unapproved_timeline_keeps_display_normalization():
    source = [_seg(1.0, 1.1), _seg(1.2, 2.0)]
    out = pipeline._effective_render_segments(
        source, duration=3.0, preserve_approved_timing=False,
    )
    assert out == pipeline._apply_display_timing(source, duration=3.0)


def test_last_line_holds_past_end_capped_at_duration():
    segs = [_seg(50.0, 52.0)]
    out = pipeline._apply_display_timing(segs, duration=53.0)
    # 52 + 4 = 56 > duration 53 → capped at 53.
    assert out[0]["end"] == 53.0


def test_last_line_extends_when_room():
    segs = [_seg(10.0, 12.0)]
    out = pipeline._apply_display_timing(segs, duration=60.0)
    assert out[0]["end"] == 12.0 + HOLD  # 16.0


def test_empty_and_single_inputs():
    assert pipeline._apply_display_timing([], 30.0) == []
    out = pipeline._apply_display_timing([_seg(1.0, 2.0)], 30.0)
    assert out[0]["end"] == 6.0  # 2 + HOLD


def test_input_not_mutated():
    segs = [_seg(10.0, 11.0), _seg(13.0, 14.0)]
    before = [dict(s) for s in segs]
    pipeline._apply_display_timing(segs, duration=60.0)
    assert segs == before, "input list must not be mutated in place"
