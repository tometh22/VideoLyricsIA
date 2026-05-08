"""Tests for the streaming-upload + backpressure refactor.

Two pieces:
 1. `_stream_upload_to_disk` writes the body to disk in chunks instead
    of buffering it all in RAM. The handler must NOT call
    `await file.read()` (no-arg) for upload endpoints, since that
    materializes the entire body — exactly the OOM path the original
    bug exposed on 30-50 MB lossless WAV uploads.
 2. `_enforce_memory_pressure` returns 503 + Retry-After when the API
    container is already close to its memory cap. The frontend's
    existing soft-fail loop (App.jsx:566-587) honours Retry-After and
    retries silently; we just need to make sure the handler raises.
"""

import io
import os
import struct
import pytest


def _wav_bytes(payload_size: int = 64) -> bytes:
    sample_data = b"\x00" * payload_size
    riff_chunk_size = 36 + len(sample_data)
    return (
        b"RIFF"
        + struct.pack("<I", riff_chunk_size)
        + b"WAVE"
        + b"fmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, 8000, 8000, 1, 8)
        + b"data"
        + struct.pack("<I", len(sample_data))
        + sample_data
    )


# ---------------------------------------------------------------------------
# 1. Streaming behaviour
# ---------------------------------------------------------------------------


def test_stream_upload_to_disk_writes_full_body_in_chunks(tmp_path, monkeypatch):
    """The streamer must write every chunk it reads, in order, and never
    issue a single `await file.read()` (the buffer-everything call)."""
    import asyncio

    import main

    # 5 MiB body → multiple 1 MiB chunks.
    payload = b"A" * (5 * 1024 * 1024 + 17)
    chunks = [payload[i:i + main._UPLOAD_CHUNK_SIZE]
              for i in range(0, len(payload), main._UPLOAD_CHUNK_SIZE)]

    class _FakeUpload:
        def __init__(self):
            self._idx = 0
            self.full_reads = 0
            self.chunked_reads = 0

        async def read(self, size=-1):
            if size in (-1, None):
                # The bug path. The streaming refactor must never call this.
                self.full_reads += 1
                return b""
            self.chunked_reads += 1
            if self._idx >= len(chunks):
                return b""
            c = chunks[self._idx]
            self._idx += 1
            return c

    fake = _FakeUpload()
    dest = tmp_path / "out.wav"

    written = asyncio.run(main._stream_upload_to_disk(fake, str(dest), max_mb=100))

    assert written == len(payload)
    assert dest.read_bytes() == payload
    assert fake.full_reads == 0, "streaming refactor must not buffer the body"
    assert fake.chunked_reads >= len(chunks)


def test_stream_upload_to_disk_413_when_over_limit(tmp_path):
    """Writing past the cap must 413 + delete the partial file before raising."""
    import asyncio

    import main
    from fastapi import HTTPException

    chunk = b"X" * main._UPLOAD_CHUNK_SIZE
    chunks = [chunk] * 10  # 10 MiB streamed under a 5 MB cap

    class _FakeUpload:
        def __init__(self):
            self._idx = 0

        async def read(self, size=-1):
            if self._idx >= len(chunks):
                return b""
            c = chunks[self._idx]
            self._idx += 1
            return c

    dest = tmp_path / "big.bin"

    with pytest.raises(HTTPException) as exc:
        asyncio.run(main._stream_upload_to_disk(_FakeUpload(), str(dest), max_mb=5))

    assert exc.value.status_code == 413
    assert not dest.exists(), "partial file should be cleaned up on 413"


def test_validate_audio_file_on_disk_passes_for_valid_wav(tmp_path):
    """Valid RIFF/WAVE header passes; magic-bytes check uses only the
    first 16 bytes from disk, not the whole body."""
    import main

    p = tmp_path / "good.wav"
    p.write_bytes(_wav_bytes(payload_size=2048))
    main._validate_audio_file_on_disk("good.wav", str(p))  # no raise


def test_validate_audio_file_on_disk_rejects_bad_header(tmp_path):
    """Renamed file → magic check fails → 400 + file unlinked."""
    import main
    from fastapi import HTTPException

    p = tmp_path / "fake.wav"
    p.write_bytes(b"NOTAWAV" * 10)

    with pytest.raises(HTTPException) as exc:
        main._validate_audio_file_on_disk("fake.wav", str(p))

    assert exc.value.status_code == 400
    assert not p.exists()


# ---------------------------------------------------------------------------
# 2. Memory + concurrency backpressure
# ---------------------------------------------------------------------------


def test_memory_gate_503_when_above_cap(monkeypatch):
    """Memory >= cap → 503 + Retry-After; matches the frontend's soft-
    fail retry loop (App.jsx:566-587)."""
    import main
    from fastapi import HTTPException

    class _FakeMem:
        percent = 90.0

    fake_psutil = type("psutil", (), {
        "virtual_memory": staticmethod(lambda: _FakeMem()),
    })
    monkeypatch.setitem(__import__("sys").modules, "psutil", fake_psutil)
    monkeypatch.setattr(main, "_MAX_MEMORY_PERCENT", 85.0)

    with pytest.raises(HTTPException) as exc:
        main._enforce_memory_pressure()
    assert exc.value.status_code == 503
    assert "Retry-After" in (exc.value.headers or {})


def test_memory_gate_silent_when_psutil_missing(monkeypatch):
    """psutil unavailable → don't block uploads. The disk gate already
    has the same fail-open contract (test_capacity_hardening covers it)."""
    import builtins
    import main

    real_import = builtins.__import__

    def _no_psutil(name, *args, **kwargs):
        if name == "psutil":
            raise ImportError("simulated")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_psutil)
    main._enforce_memory_pressure()  # no raise


def test_upload_slot_no_op_when_redis_missing(monkeypatch):
    """Without REDIS_URL we skip enforcement (dev / tests) and return None
    instead of an HTTP error — the in-process memory gate still applies."""
    import main

    monkeypatch.delenv("REDIS_URL", raising=False)
    assert main._try_acquire_upload_slot() is None
    main._release_upload_slot(None)  # no raise


def test_upload_slot_503_when_cap_reached(monkeypatch):
    """When the Redis-shared counter is already at the cap, the next
    acquire raises 503 with Retry-After and removes the leased entry
    so it doesn't leak."""
    import main
    from fastapi import HTTPException

    class _FakePipe:
        def __init__(self, count):
            self._count = count
            self._calls = []

        def sadd(self, *a, **k): self._calls.append(("sadd", a)); return self
        def scard(self, *a, **k): self._calls.append(("scard", a)); return self
        def expire(self, *a, **k): self._calls.append(("expire", a)); return self
        def execute(self):
            return [1, self._count, 1]

    class _FakeRedis:
        srem_calls = []
        def __init__(self, *a, **k): pass
        @classmethod
        def from_url(cls, *a, **k): return cls()
        def pipeline(self): return _FakePipe(count=99)
        def srem(self, key, lease):
            _FakeRedis.srem_calls.append((key, lease))

    import sys
    fake_module = type("redis", (), {"Redis": _FakeRedis})
    monkeypatch.setitem(sys.modules, "redis", fake_module)
    monkeypatch.setenv("REDIS_URL", "redis://fake:6379")
    monkeypatch.setattr(main, "_MAX_CONCURRENT_UPLOADS", 8)

    with pytest.raises(HTTPException) as exc:
        main._try_acquire_upload_slot()
    assert exc.value.status_code == 503
    assert "Retry-After" in (exc.value.headers or {})
    # Lease was added but rolled back via SREM so we don't leak slots.
    assert _FakeRedis.srem_calls, "lease must be released on cap rejection"
