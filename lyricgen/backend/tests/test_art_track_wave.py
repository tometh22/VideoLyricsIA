"""Unit tests for the audio-reactive art-track wave module (pure DSP+draw).

No ffmpeg and no real audio files: librosa is only exercised through
monkeypatched entry points or tiny synthetic signals, so the suite runs
fast anywhere the backend deps are installed.
"""

import numpy as np
import pytest

import art_track_wave as atw


# ---------------------------------------------------------------------------
# _attack_release
# ---------------------------------------------------------------------------

def _impulse(n_frames=40, n_bars=3, at=5):
    x = np.zeros((n_frames, n_bars), dtype=np.float32)
    x[at] = 1.0  # single-frame hit
    return x


def test_attack_snaps_up_within_two_frames():
    x = np.zeros((10, 1), dtype=np.float32)
    x[5:] = 1.0  # step
    env = atw._attack_release(x, fps=24.0)
    assert env[6, 0] >= 0.9  # within 2 frames of the step


def test_release_falls_with_gravity():
    env = atw._attack_release(_impulse(), fps=24.0)
    peak = env[5, 0]
    assert peak > 0.5
    # ~12 frames (0.5 s) later the bar has fallen below 15% of nothing-new
    assert env[17, 0] < 0.15
    # and the fall is monotonic (no bounce)
    assert np.all(np.diff(env[5:17, 0]) <= 1e-6)


def test_release_is_fps_invariant_in_seconds():
    # Same 0.5 s after the hit, 24fps and 30fps must agree (±10%)
    e24 = atw._attack_release(_impulse(60, at=10), fps=24.0)
    e30 = atw._attack_release(_impulse(60, at=10), fps=30.0)
    v24 = e24[10 + 12, 0]   # 12 frames @24fps = 0.5s
    v30 = e30[10 + 15, 0]   # 15 frames @30fps = 0.5s
    assert v24 == pytest.approx(v30, rel=0.10)


# ---------------------------------------------------------------------------
# _per_band_normalize
# ---------------------------------------------------------------------------

def test_per_band_normalize_kills_bass_wall():
    # 20 dB gap: hotter bass, but within the 25 dB dead-band guard (a gap
    # beyond the guard is deliberately treated as a near-silent band).
    rng = np.random.default_rng(7)
    treble = rng.uniform(-40.0, -20.0, 200)
    bass = treble + 20.0  # bass band 20 dB hotter everywhere
    S_db = np.stack([bass, treble])
    h = atw._per_band_normalize(S_db)
    # Both bands' loud moments reach ~the same height despite the 30 dB gap
    assert np.percentile(h[0], 98) == pytest.approx(
        np.percentile(h[1], 98), abs=0.05)
    assert h.max() <= 1.0 and h.min() >= 0.0


def test_per_band_normalize_dead_band_guard():
    loud = np.full(100, -10.0)
    dead = np.full(100, -70.0)  # 60 dB down: hiss-level
    h = atw._per_band_normalize(np.stack([loud, dead]))
    assert h[0].max() > 0.9          # hot band fills the box
    assert h[1].max() < 0.3          # dead band stays low, not amplified


def test_silent_window_is_spine_not_white_block():
    # A near-silent window (every band at the -100 dB floor) must map to
    # all-zero heights (→ spine), NOT to 1.0 (a solid white block).
    S_db = np.full((48, 100), -100.0, dtype=np.float32)
    h = atw._per_band_normalize(S_db)
    assert float(h.max()) == 0.0


def test_quiet_intro_stays_low_globally():
    # Global (not rolling) normalization: a -35 dB intro before a 0 dB
    # chorus must render clearly lower than the chorus.
    band = np.concatenate([np.full(100, -35.0), np.full(100, 0.0)])
    h = atw._per_band_normalize(band[None, :])
    assert h[0, :100].max() < 0.35
    assert h[0, 100:].max() > 0.9


# ---------------------------------------------------------------------------
# compute_bar_frames
# ---------------------------------------------------------------------------

def test_compute_bar_frames_exact_frame_count_ntsc(monkeypatch):
    sr = atw._SR
    t = np.linspace(0, 10.0, int(sr * 10.0), endpoint=False)
    y = (0.5 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    import librosa
    monkeypatch.setattr(librosa, "load", lambda *a, **k: (y, sr))
    fps = 24000 / 1001
    n_frames = int(np.ceil(10.0 * fps))  # 240
    out = atw.compute_bar_frames("x.mp3", n_bars=48, n_frames=n_frames, fps=fps)
    assert out.shape == (n_frames, 48)
    assert out.dtype == np.float32
    assert float(out.min()) >= 0.0 and float(out.max()) <= 1.0


def test_compute_bar_frames_falls_back_and_never_raises(monkeypatch):
    import librosa
    def boom(*a, **k):
        raise RuntimeError("decoder exploded")
    monkeypatch.setattr(librosa, "load", boom)
    out = atw.compute_bar_frames("nope.mp3", n_bars=48, n_frames=120, fps=24.0)
    assert out.shape == (120, 48)
    assert float(out.min()) > 0.0          # flat fallback breathes, never dead
    assert float(out.max()) <= 1.0


def test_static_fallback_breathes():
    out = atw.static_bar_frames(48, 8, 24.0, mp3_path=None)
    # same bar changes height over time (the sine breathing)
    assert out[:, 3].std() > 0.005


# ---------------------------------------------------------------------------
# draw_wave_frame / sprite writer
# ---------------------------------------------------------------------------

W, H, PITCH, BAR_W = 960, 150, 20, 12


def test_draw_wave_frame_geometry_and_symmetry():
    heights = np.linspace(0.0, 1.0, 48)
    img = atw.draw_wave_frame(heights, W, H, pitch=PITCH, bar_w=BAR_W)
    assert img.size == (W, H) and img.mode == "RGBA"
    a = np.asarray(img)[:, :, 3]
    # Mirrored from center: alpha symmetric around the horizontal midline
    assert np.array_equal(a[: H // 2], a[H // 2:][::-1])
    # Slot boundaries stay transparent (bars are separate, not a blob)
    x_off = (W - 48 * PITCH) // 2
    assert a[:, x_off] .max() == 0  # first column of the first slot is margin


def test_draw_wave_frame_zero_heights_keeps_spine():
    img = atw.draw_wave_frame(np.zeros(48), W, H, pitch=PITCH, bar_w=BAR_W)
    a = np.asarray(img)[:, :, 3]
    cy = H // 2
    assert a[cy].max() > 0          # spine present at the center line
    assert a[10].max() == 0          # nothing near the top edge


def test_sprite_writer_matches_reference():
    rng = np.random.default_rng(3)
    heights = rng.uniform(0.0, 1.0, 48)
    ref = np.asarray(atw.draw_wave_frame(heights, W, H, pitch=PITCH, bar_w=BAR_W))
    writer = atw.WaveFrameWriter(48, W, H, pitch=PITCH, bar_w=BAR_W)
    fast = np.asarray(writer.frame(heights))
    assert np.array_equal(ref[:, :, 3], fast[:, :, 3])  # identical alpha
    # where visible, both are pure white
    vis = fast[:, :, 3] > 0
    assert np.all(fast[:, :, :3][vis] == 255)


def test_write_wave_frames_writes_sequence(tmp_path):
    frames = np.tile(np.linspace(0, 1, 24, dtype=np.float32), (5, 1))
    pattern = atw.write_wave_frames(frames, str(tmp_path / "wave"),
                                    w=480, h=80, pitch=20, bar_w=12)
    assert pattern.endswith("w%06d.png")
    files = sorted((tmp_path / "wave").iterdir())
    assert [f.name for f in files] == [f"w{i:06d}.png" for i in range(5)]


# ---------------------------------------------------------------------------
# spanish_smart_title (lives in pipeline.py, pure string helper)
# ---------------------------------------------------------------------------

def test_spanish_smart_title_cases():
    from pipeline import spanish_smart_title
    assert (spanish_smart_title("La Leyenda Del Hada Y El Mago")
            == "La Leyenda del Hada y el Mago")
    # first word untouched even if a stopword
    assert spanish_smart_title("Del Barrio") == "Del Barrio"
    # deliberate ALLCAPS preserved
    assert spanish_smart_title("Fuera DEL Mundo") == "Fuera DEL Mundo"
    # English titles unaffected
    assert (spanish_smart_title("The Man Who Sold The World")
            == "The Man Who Sold The World")
    assert spanish_smart_title("") == ""
