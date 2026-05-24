"""Tests for `vocal_sep.validate_stem` — pre-flight check that catches
the degenerate stem shapes that crash forced_align (Bug C, incident
2026-05-24 Mujer Amante).

The validator is called from `vocal_sep.separate_vocals` (after demucs
download) and from `forced_align.forced_align_lyrics` (defensive layer
for raw user audio). When it returns `False`, callers fall through to
the audio mix — a safe known-working path.
"""
import subprocess

import pytest

import vocal_sep


def _make_wav(path: str, *, duration_s: float, sample_rate: int = 44100,
              channels: int = 1):
    """Generate a WAV via ffmpeg (lavfi sine generator). We use ffmpeg
    here because we already require it on the worker; manual `wave`
    module fixtures end up subtly different from real demucs output and
    sometimes pass the validator when they shouldn't."""
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi",
         "-i", f"sine=frequency=440:duration={duration_s}",
         "-ar", str(sample_rate), "-ac", str(channels), path],
        check=True, capture_output=True,
    )


def test_rejects_missing_path():
    ok, reason = vocal_sep.validate_stem("/nonexistent/path.wav")
    assert ok is False
    assert reason == "missing"


def test_rejects_tiny_file(tmp_path):
    """Bug C regression: < 10 KB cannot be 3+ s of audio."""
    p = tmp_path / "tiny.wav"
    p.write_bytes(b"\x00" * 100)
    ok, reason = vocal_sep.validate_stem(str(p))
    assert ok is False
    assert "too_small" in reason


def test_rejects_very_short_audio(tmp_path):
    """The [1,2,1] tensor shape that crashed Mujer Amante: a sub-second
    audio file. Generate a real WAV header so we exercise the
    duration-too-short branch (not the size branch)."""
    p = tmp_path / "shortish.wav"
    # 500 ms stereo 44.1 kHz → ~88 KB (passes size check, fails duration)
    _make_wav(str(p), duration_s=0.5, sample_rate=44100, channels=2)
    ok, reason = vocal_sep.validate_stem(str(p))
    assert ok is False
    assert "duration_too_short" in reason


def test_accepts_normal_audio(tmp_path):
    """A 5 s mono 44.1 kHz file passes (typical post-demucs stem shape)."""
    p = tmp_path / "ok.wav"
    _make_wav(str(p), duration_s=5.0, sample_rate=44100, channels=1)
    ok, reason = vocal_sep.validate_stem(str(p))
    assert ok is True
    assert reason == "ok"


def test_accepts_stereo_48k(tmp_path):
    """Stereo 48 kHz (common demucs output for high-res masters) is OK."""
    p = tmp_path / "stereo48k.wav"
    _make_wav(str(p), duration_s=5.0, sample_rate=48000, channels=2)
    ok, reason = vocal_sep.validate_stem(str(p))
    assert ok is True


def test_forced_align_aborts_on_invalid_stem(monkeypatch, tmp_path):
    """Bug C end-to-end: forced_align_lyrics MUST NOT call Replicate
    when the input audio fails pre-flight validation. The crashing
    `padding (200,200)` error happens server-side, so the only way to
    prevent it is to skip the upload entirely."""
    import forced_align

    p = tmp_path / "bad.wav"
    p.write_bytes(b"\x00" * 100)  # < 10 KB → fails validation

    # Mock replicate.run to record any call and fail the test if invoked.
    called = []
    def _spy(*args, **kwargs):
        called.append((args, kwargs))
        return None
    monkeypatch.setattr("replicate.run", _spy, raising=False)
    monkeypatch.setenv("FORCED_ALIGNER_ENABLED", "1")
    monkeypatch.setenv("REPLICATE_API_TOKEN", "r8_test")

    result = forced_align.forced_align_lyrics(
        str(p), "line one\nline two\nline three\nline four")

    assert result is None, "should bail out, not call Replicate"
    assert called == [], "Replicate must NOT be called with invalid audio"
