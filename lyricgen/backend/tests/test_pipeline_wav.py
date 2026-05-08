"""Tests for `_compress_for_whisper` after the silent-fallback fix.

The old code swallowed any ffmpeg failure and returned the original
30-50 MB WAV; the Whisper API would then reject it (>25 MB) and the
job error came out as an opaque OpenAI client message. After the fix,
ffmpeg failures raise RuntimeError("audio_compression_failed: …") so
the pipeline catch-all in pipeline.py:506-522 surfaces the real reason
to the operator (job.error + Sentry tag).
"""

import subprocess as sp
import struct

import pytest


def _wav_bytes(payload_size: int = 64) -> bytes:
    sample_data = b"\x00" * payload_size
    return (
        b"RIFF"
        + struct.pack("<I", 36 + len(sample_data))
        + b"WAVE"
        + b"fmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, 8000, 8000, 1, 8)
        + b"data"
        + struct.pack("<I", len(sample_data))
        + sample_data
    )


def test_compress_for_whisper_passthrough_when_small(tmp_path):
    """File already under the 25 MB cap → return the input path
    unchanged. No ffmpeg invocation, no temp file."""
    from pipeline import _compress_for_whisper

    src = tmp_path / "small.wav"
    src.write_bytes(_wav_bytes(payload_size=1024))

    out = _compress_for_whisper(str(src))
    assert out == str(src)


def test_compress_for_whisper_raises_when_ffmpeg_fails(tmp_path, monkeypatch):
    """Large input + ffmpeg failure → RuntimeError with audio_compression_failed."""
    import pipeline

    src = tmp_path / "big.wav"
    src.write_bytes(b"\x00" * 4096)  # actual size doesn't matter — we mock getsize

    monkeypatch.setattr(pipeline.os.path, "getsize", lambda p: 30_000_000)

    def _fail(*args, **kwargs):
        raise sp.CalledProcessError(
            returncode=1, cmd="ffmpeg",
            stderr="ffmpeg fake error: invalid data",
        )
    monkeypatch.setattr("subprocess.run", _fail)

    with pytest.raises(RuntimeError) as exc:
        pipeline._compress_for_whisper(str(src))
    msg = str(exc.value)
    assert "audio_compression_failed" in msg
    assert "30.0 MB" in msg or "30 MB" in msg
    assert "ffmpeg fake error" in msg


def test_compress_for_whisper_raises_when_output_empty(tmp_path, monkeypatch):
    """ffmpeg returned 0 but produced a 0-byte / missing file → still raise.
    Matches the silent-success-but-corrupt-output failure mode (disk full,
    OOM mid-encode) that previously slipped through."""
    import pipeline

    src = tmp_path / "big.wav"
    src.write_bytes(b"\x00" * 4096)

    monkeypatch.setattr(pipeline.os.path, "getsize", lambda p: 30_000_000)

    # ffmpeg exits 0 but doesn't actually create the output file.
    monkeypatch.setattr("subprocess.run", lambda *a, **k: None)
    monkeypatch.setattr(pipeline.os.path, "exists", lambda p: False)

    with pytest.raises(RuntimeError) as exc:
        pipeline._compress_for_whisper(str(src))
    assert "audio_compression_failed" in str(exc.value)


def test_compress_for_whisper_raises_when_ffmpeg_missing(tmp_path, monkeypatch):
    """ffmpeg binary not on PATH → FileNotFoundError → wrapped as
    RuntimeError("audio_compression_failed:…") so the pipeline catch
    sees a clear message."""
    import pipeline

    src = tmp_path / "big.wav"
    src.write_bytes(b"\x00" * 4096)

    monkeypatch.setattr(pipeline.os.path, "getsize", lambda p: 30_000_000)
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("ffmpeg")),
    )

    with pytest.raises(RuntimeError) as exc:
        pipeline._compress_for_whisper(str(src))
    assert "audio_compression_failed" in str(exc.value)
