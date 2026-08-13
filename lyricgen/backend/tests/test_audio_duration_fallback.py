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


# ---------------------------------------------------------------------------
# Regression tests for the 2026-08-12 audit follow-up (jobs 878d99b8da76 /
# 51fac94587cd / 072f9646c349 — Sentry PYTHON-FASTAPI-3F):
#
#   F1: the header-only fast path (wave.getnframes / mutagen) was only
#       bounded against an ABSOLUTE ceiling (<=3600s), never cross-checked
#       against a real demux. A WAV whose `data` chunk size lies "plausibly"
#       (e.g. reports 300s for 10s of real audio) sailed through the ceiling
#       and still broke _verify_deliverables downstream — same bug, smaller
#       window. Fixed by cross-checking the header value against
#       _ffprobe_duration() with the same 2s tolerance _verify_deliverables
#       itself uses.
#
#   F5: the /edit re-render path had `try: audio_dur = _audio_duration(...)
#       except Exception: audio_dur = _ffprobe_duration(...)` — dead code,
#       since _audio_duration never raises. On the one path that actually
#       failed, _verify_deliverables received audio_dur=None and silently
#       skipped the whole duration check (no Sentry event, no log). Fixed
#       by mirroring run_pipeline's explicit `if audio_dur is None:` check.
# ---------------------------------------------------------------------------

def _make_wav_with_declared_duration(path, real_seconds, declared_seconds,
                                      sample_rate=44100, channels=2, bits=16):
    """Build a real, playable WAV of `real_seconds` audio whose `data` chunk
    header LIES about its size so a header-only reader computes
    `declared_seconds` instead. Reproduces the corrupted/placeholder-length
    WAV header pattern found in production (sentinel 0xFFFFFFFE == "unknown
    length, patch me later" — some streaming/live-capture exporters never
    rewrite it)."""
    block_align = channels * bits // 8
    byte_rate = sample_rate * block_align
    real_data = b"\x00\x00" * channels * int(sample_rate * real_seconds)
    declared_data_size = int(sample_rate * declared_seconds) * block_align

    header = b"RIFF" + struct.pack("<I", 36 + len(real_data)) + b"WAVE"
    header += b"fmt " + struct.pack(
        "<IHHIIHH", 16, 1, channels, sample_rate, byte_rate, block_align, bits,
    )
    header += b"data" + struct.pack("<I", declared_data_size)  # <-- the lie
    path.write_bytes(header + real_data)


def test_audio_duration_crosscheck_catches_plausible_header_lie(tmp_path):
    """F1: a header that lies within the sane-ceiling window must not be
    trusted — the real (ffprobe-demuxed) duration must win."""
    pipeline = __import__("pytest").importorskip("pipeline",
        reason="pipeline deps (librosa, numpy, moviepy) not installed")

    wav = tmp_path / "plausible_lie.wav"
    _make_wav_with_declared_duration(wav, real_seconds=10, declared_seconds=300)

    result = pipeline._audio_duration(str(wav))
    assert result is not None
    assert abs(result - 10.0) < 0.5, (
        f"header lied 300s for 10s of real audio and the lie won: got {result!r}. "
        "This is the exact deterministic, retry-proof failure mode from the "
        "878d99b8da76/51fac94587cd/072f9646c349 incident, just under the "
        "3600s ceiling instead of over it."
    )


def test_audio_duration_crosscheck_catches_original_sentinel_bug(tmp_path):
    """F1 regression: the original production incident — WAV `data` chunk
    size corrupted to the 0xFFFFFFFE 'unknown length' sentinel, which
    wave.getnframes() took at face value as ~24347.9s for a 2s file."""
    pipeline = __import__("pytest").importorskip("pipeline",
        reason="pipeline deps (librosa, numpy, moviepy) not installed")
    import wave as _wave_mod

    sample_rate, channels, bits = 44100, 2, 16
    real_data = b"\x00\x00" * channels * (sample_rate * 2)  # 2s real audio
    block_align = channels * bits // 8
    byte_rate = sample_rate * block_align
    header = b"RIFF" + struct.pack("<I", 36 + len(real_data)) + b"WAVE"
    header += b"fmt " + struct.pack(
        "<IHHIIHH", 16, 1, channels, sample_rate, byte_rate, block_align, bits,
    )
    header += b"data" + struct.pack("<I", 0xFFFFFFFE)  # production sentinel
    wav = tmp_path / "corrupt_sentinel.wav"
    wav.write_bytes(header + real_data)

    # Confirm the raw stdlib bug still reproduces (i.e. this test would have
    # caught the original incident) before checking our fix catches it.
    with _wave_mod.open(str(wav), "rb") as wf:
        raw_dur = wf.getnframes() / wf.getframerate()
    assert raw_dur > 20000, (
        "wave.getnframes() no longer reproduces the sentinel bug — test "
        "fixture may be stale, re-derive against a fresh production sample"
    )

    result = pipeline._audio_duration(str(wav))
    assert result is not None
    assert abs(result - 2.0) < 0.5, f"expected ~2.0s (real audio), got {result!r}"


def test_edit_path_verify_call_uses_explicit_none_check_not_dead_except():
    """F5 source-level guard: every `_audio_duration(mp3_path)` call
    immediately verified by `_verify_deliverables` must fall back to
    `_ffprobe_duration` via an explicit `is None` check, not a `try/except
    Exception` — `_audio_duration` never raises (it catches internally and
    returns None), so `except Exception` around it is always dead code, and
    the None it silently lets through disables `_verify_deliverables`'s
    entire duration check (`if expected_dur is not None:`) with no error
    anywhere. Reads raw source so it works without the full dep tree."""
    with open(_PIPELINE_SRC, encoding="utf-8") as f:
        src = f.read()
    dead_pattern = (
        "try:\n            audio_dur = _audio_duration(mp3_path)\n"
        "        except Exception:\n            audio_dur = _ffprobe_duration(mp3_path)"
    )
    assert dead_pattern not in src, (
        "dead try/except re-introduced around _audio_duration(mp3_path) — "
        "_audio_duration never raises, so this silently lets None reach "
        "_verify_deliverables and disables the duration check with no error "
        "(the exact bug fixed in the /edit re-render path, audit 2026-08-12)"
    )
