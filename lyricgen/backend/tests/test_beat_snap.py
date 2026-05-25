"""Unit tests for beat_snap (pure parts — no librosa I/O)."""

import beat_snap as bs


def test_is_enabled_default_off(monkeypatch):
    monkeypatch.delenv("BEAT_SNAP_ENABLED", raising=False)
    assert bs.is_enabled() is False
    monkeypatch.setenv("BEAT_SNAP_ENABLED", "1")
    assert bs.is_enabled() is True


def test_snap_segments_snaps_to_nearest_beat_within_window():
    # 120 BPM → beats every 0.5s. Segment starts a touch off the beat:
    # 0.55 should snap to 0.5, 1.43 should snap to 1.5 (both within ±200ms).
    beats = [0.0, 0.5, 1.0, 1.5, 2.0, 2.5]
    segs = [
        {"start": 0.55, "end": 1.50, "text": "uno"},
        {"start": 1.65, "end": 2.40, "text": "dos"},   # nearest is 1.5 (Δ=-150ms)
    ]
    out = bs.snap_segments(segs, beats, window_ms=200)
    # First snaps to 0.5
    assert out[0]["start"] == 0.5
    # Second nearest beat is 1.5 (Δ=-150) — but prev_end is 1.5, so floor=1.505
    # Within window 200ms of 1.5 the snap target IS 1.5 but clamped to 1.505.
    # Actually nearest to 1.65 is 1.5 (Δ-150) vs 2.0 (Δ+350). Pick 1.5.
    # 1.5 < prev_end (1.5) so floor to 1.505.
    assert 1.5 < out[1]["start"] <= 1.51


def test_snap_segments_leaves_alone_when_outside_window():
    beats = [0.0, 1.0, 2.0, 3.0]   # very wide spacing
    segs = [{"start": 1.4, "end": 1.9, "text": "x"}]
    out = bs.snap_segments(segs, beats, window_ms=100)
    # Nearest is 1.0 (Δ-400) or 2.0 (Δ+600) — both outside ±100ms → no snap.
    assert out[0]["start"] == 1.4


def test_snap_segments_never_overlaps_previous():
    beats = [0.0, 0.5, 1.0]
    segs = [
        {"start": 0.49, "end": 0.95, "text": "a"},   # snaps to 0.5
        {"start": 1.05, "end": 1.50, "text": "b"},   # nearest 1.0 within window,
                                                       # but prev_end is 0.95 → clamp
    ]
    out = bs.snap_segments(segs, beats, window_ms=200)
    assert out[0]["start"] == 0.5
    assert out[1]["start"] >= 0.95   # no overlap


def test_snap_segments_never_pushes_past_own_end():
    # Pathological: start near end with a beat just after. Snap would push
    # start past end — must be refused.
    beats = [1.0, 2.0]
    segs = [{"start": 0.95, "end": 0.98, "text": "tiny"}]
    out = bs.snap_segments(segs, beats, window_ms=200)
    assert out[0]["start"] == 0.95   # untouched


def test_snap_segments_empty_inputs():
    assert bs.snap_segments([], [0.5, 1.0]) == []
    segs = [{"start": 0.5, "end": 1.0, "text": "x"}]
    assert bs.snap_segments(segs, []) == segs


def test_snap_segments_window_zero_is_noop():
    beats = [0.0, 0.5, 1.0]
    segs = [{"start": 0.49, "end": 0.95, "text": "x"}]
    assert bs.snap_segments(segs, beats, window_ms=0) == segs


def test_apply_passes_through_when_disabled(monkeypatch):
    monkeypatch.delenv("BEAT_SNAP_ENABLED", raising=False)
    segs = [{"start": 0.5, "end": 1.0, "text": "x"}]
    assert bs.apply("/tmp/nope.wav", segs) == segs


def test_apply_passes_through_on_detect_failure(monkeypatch):
    monkeypatch.setenv("BEAT_SNAP_ENABLED", "1")
    segs = [{"start": 0.5, "end": 1.0, "text": "x"}]
    # Nonexistent path → librosa.load raises → detect_beats returns None →
    # apply returns the originals unchanged. Never raises.
    out = bs.apply("/no/such/audio.wav", segs)
    assert out == segs
