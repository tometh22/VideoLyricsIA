"""Audio duration fallback chain — regression tests for Bug 1 in pipeline.py.

Original code used 0.0 as the except-clause fallback when _audio_duration()
raised. Because _audio_duration() never raises (it catches all exceptions
internally and returns None), the 0.0 branch was dead code. However, if ever
triggered it would cause _verify_deliverables to fail for every real video
(abs(actual_duration - 0.0) > 2.0 is always True for any song > 2 seconds).

After the fix the fallback chain is:
  _audio_duration() → None → _ffprobe_duration() → None → skip duration check

Tests here work in two modes:
  - Without full pipeline deps (bare env): source-inspection + inline logic tests
  - With full pipeline deps (Docker): also exercises real _audio_duration
"""

import os
import struct


# ---------------------------------------------------------------------------
# Source-level regression guard (no pipeline import needed)
# ---------------------------------------------------------------------------

_PIPELINE_SRC = os.path.join(os.path.dirname(__file__), "..", "pipeline.py")


def test_no_zero_fallback_in_pipeline_source():
    """Guard against re-introducing the 0.0 fallback bug.

    Before the fix: `except Exception: audio_dur_for_verify = 0.0`
    would cause _verify_deliverables to fail for ALL videos > 2 seconds.
    This test reads the raw source so it works without the full dep tree.
    """
    with open(_PIPELINE_SRC, encoding="utf-8") as f:
        src = f.read()
    assert "audio_dur_for_verify = 0.0" not in src, (
        "0.0 fallback re-introduced in pipeline.py — "
        "this causes _verify_deliverables to fail for ALL videos > 2 s"
    )


def test_ffprobe_fallback_present_in_pipeline_source():
    """The _ffprobe_duration fallback must follow the _audio_duration call."""
    with open(_PIPELINE_SRC, encoding="utf-8") as f:
        src = f.read()
    assert "audio_dur_for_verify = _ffprobe_duration" in src or \
           "audio_dur_for_verify = _ffprobe_duration(mp3_path)" in src, (
        "_ffprobe_duration fallback not found in pipeline.py after _audio_duration"
    )


# ---------------------------------------------------------------------------
# Inline fallback chain logic — tests the 7-line block without importing pipeline
# ---------------------------------------------------------------------------

def _simulate_fallback(audio_dur_return, probe_return):
    """Replicate the pipeline.py:529-535 block with injectable results."""
    def _audio_duration(_path):
        return audio_dur_return

    def _ffprobe_duration(_path):
        return probe_return

    verify_received = []

    def _verify_deliverables(_job_dir, _files, dur):
        verify_received.append(dur)

    # --- This is the exact block from pipeline.py:529-535 ---
    try:
        audio_dur_for_verify = _audio_duration("audio.wav")
    except Exception:
        audio_dur_for_verify = None
    if audio_dur_for_verify is None:
        audio_dur_for_verify = _ffprobe_duration("audio.wav")
    _verify_deliverables(".", {}, audio_dur_for_verify)
    # ---------------------------------------------------------

    return verify_received[0] if verify_received else None


def test_ffprobe_used_when_audio_duration_returns_none():
    """When _audio_duration returns None, _ffprobe_duration provides the duration."""
    result = _simulate_fallback(audio_dur_return=None, probe_return=180.0)
    assert result == 180.0


def test_audio_duration_used_directly_when_successful():
    """When _audio_duration succeeds, that value is used directly."""
    result = _simulate_fallback(audio_dur_return=210.5, probe_return=999.0)
    assert result == 210.5, f"Expected 210.5, got {result}"


def test_none_passed_to_verify_when_both_fail():
    """When both fail, None is passed — duration check is skipped (not false-failed)."""
    result = _simulate_fallback(audio_dur_return=None, probe_return=None)
    assert result is None, (
        f"Both failed: expected None (skip check) not 0.0 (false failure). Got: {result!r}"
    )


def test_zero_would_fail_2s_plus_video():
    """Confirm the pre-fix 0.0 bug: it would cause a false failure for normal videos.

    This test documents WHY 0.0 was dangerous. With the old code:
      except Exception: audio_dur_for_verify = 0.0
    → _verify_deliverables sees expected_dur=0.0
    → abs(180.0 - 0.0) > 2.0 → True → RuntimeError('duration 180.0s differs from audio 0.0s')

    The test below confirms this math — it's NOT testing the fixed code, it's proving
    why 0.0 would have been wrong.
    """
    actual_video_duration = 180.0  # 3-minute song
    old_fallback = 0.0
    tolerance = 2.0
    would_have_raised = abs(actual_video_duration - old_fallback) > tolerance
    assert would_have_raised, (
        "Math check failed: 0.0 fallback should cause a false failure for a 180s video"
    )


# ---------------------------------------------------------------------------
# Tests that import pipeline directly — skipped if heavy deps are missing
# ---------------------------------------------------------------------------

def test_audio_duration_returns_none_on_corrupt_file(tmp_path):
    pipeline = __import__("pytest").importorskip("pipeline",
        reason="pipeline deps (librosa, numpy, moviepy) not installed")

    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"this is not a wav")
    result = pipeline._audio_duration(str(bad))
    assert result is None, f"expected None for corrupt WAV, got {result!r}"


def test_audio_duration_returns_float_for_valid_wav(tmp_path):
    pipeline = __import__("pytest").importorskip("pipeline",
        reason="pipeline deps (librosa, numpy, moviepy) not installed")

    samples = b"\x00" * 8000
    wav_data = (
        b"RIFF" + struct.pack("<I", 36 + len(samples)) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, 8000, 8000, 1, 8)
        + b"data" + struct.pack("<I", len(samples)) + samples
    )
    wav = tmp_path / "ok.wav"
    wav.write_bytes(wav_data)
    result = pipeline._audio_duration(str(wav))
    assert result is not None
    assert abs(result - 1.0) < 0.05, f"expected ~1.0 s, got {result}"
