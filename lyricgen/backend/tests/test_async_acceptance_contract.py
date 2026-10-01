import asyncio
import time

import pytest
from fastapi import HTTPException


def test_optional_storage_probe_has_a_hard_deadline(monkeypatch):
    import main

    async def never_returns(_callable, *_args):
        await asyncio.sleep(60)

    monkeypatch.setattr(main.asyncio, "to_thread", never_returns)
    started = time.monotonic()
    result = asyncio.run(
        main._bounded_storage_probe(lambda: True, timeout_seconds=0.01)
    )

    assert result is None
    assert time.monotonic() - started < 0.25


def test_worker_materialized_file_enforces_real_size(monkeypatch, tmp_path):
    import main

    audio = tmp_path / "oversized.wav"
    audio.write_bytes(b"RIFF" + (b"0" * (1024 * 1024)))
    monkeypatch.setattr(main, "MAX_UPLOAD_MB", 1)

    with pytest.raises(HTTPException) as exc:
        main._validate_audio_file_on_disk(audio.name, str(audio))

    assert exc.value.status_code == 413
    assert not audio.exists()
