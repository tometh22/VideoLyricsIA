"""Tests for the `locked` flag in _apply_display_timing.

A segment marked `locked: true` was hand-timed by the operator in the
visual Timings editor. The render must respect that end (only the
no-overlap safety clamp applies), NOT auto-extend it via hold-until-next.
Untouched (unlocked) segments keep the default karaoke hold. See the
visual-timings-editor plan + UMG "Costumbres argentinas" 2026-05-19.
"""

import pipeline

GAP = 0.05
HOLD = 4.0


def _seg(start, end, text="x", **extra):
    return {"start": start, "end": end, "text": text, **extra}


def test_locked_short_end_is_not_auto_extended():
    """The whole point: a locked line with a deliberately SHORT end, with a
    big gap after it, must keep that short end — NOT hold-until-next."""
    segs = [_seg(10.0, 11.0, locked=True), _seg(40.0, 41.0)]  # 29s gap after
    out = pipeline._apply_display_timing(segs, duration=60.0)
    assert out[0]["end"] == 11.0, out[0]  # respected, not extended to 15 or 40


def test_unlocked_neighbor_still_auto_extends():
    """A locked line must not disable the auto-hold for its unlocked
    neighbors — the flag is per-segment."""
    segs = [_seg(10.0, 11.0, locked=True), _seg(13.0, 14.0)]  # 2nd unlocked, gap after
    out = pipeline._apply_display_timing(segs, duration=60.0)
    assert out[0]["end"] == 11.0          # locked: untouched
    assert out[1]["end"] == 14.0 + HOLD   # unlocked last line: held +4s


def test_locked_long_end_clamped_to_no_overlap():
    """A locked end that would overrun the next line is still clamped — a
    manual end can't be allowed to render two lines at once."""
    segs = [_seg(10.0, 20.0, locked=True), _seg(15.0, 16.0)]  # locked end past next.start
    out = pipeline._apply_display_timing(segs, duration=60.0)
    assert out[0]["end"] == 15.0 - GAP, out[0]  # clamped to next.start - gap


def test_locked_flag_preserved_in_output():
    segs = [_seg(10.0, 12.0, locked=True), _seg(20.0, 21.0)]
    out = pipeline._apply_display_timing(segs, duration=60.0)
    assert out[0].get("locked") is True
    assert "locked" not in out[1]  # untouched line stays clean


def test_unlocked_unchanged_regression():
    """No locked keys anywhere → identical to the pre-locked behaviour."""
    segs = [_seg(0.0, 1.0), _seg(13.0, 14.0)]
    out = pipeline._apply_display_timing(segs, duration=60.0)
    assert out[0]["end"] == 1.0 + HOLD   # 5.0
    assert out[1]["end"] == 14.0 + HOLD  # 18.0


def test_locked_last_line_respected():
    segs = [_seg(50.0, 52.0, locked=True)]
    out = pipeline._apply_display_timing(segs, duration=60.0)
    assert out[0]["end"] == 52.0  # not extended +4s, respects manual end
