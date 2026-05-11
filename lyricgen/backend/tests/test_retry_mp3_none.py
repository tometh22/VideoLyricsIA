"""Retry endpoint passes mp3_path=None — the pipeline must derive it.

Incident: 2026-05-11 22:43 UTC. Admin clicked "Reintentar sin re-subir"
on two reaped jobs. The /retry/{job_id} endpoint reads input_r2_key
from the DB row, then calls enqueue_pipeline(mp3_path=None, ...) on
the assumption the worker will materialize the audio from R2 on its
own. But pipeline.py:run_pipeline checks `if not os.path.exists(mp3_path)`
BEFORE handling the None case → TypeError → RQ retry → same crash →
pipeline_failure_callback flips the row to error → user thinks the
retry button is broken.

These tests pin the recovery path: a worker called with mp3_path=None
and a real input_r2_key derives a local path under OUTPUTS_DIR/job_id
using the R2 key's basename, THEN proceeds with the R2 download.
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest


def test_retry_path_derived_from_r2_key_basename(tmp_path, monkeypatch):
    """When mp3_path is None but input_r2_key is set, the pipeline
    derives mp3_path = <job_dir>/<basename(input_r2_key)> before any
    os.path.exists check that would crash on None."""
    # Stub OUTPUTS_DIR to a tmp dir so makedirs + path joins are safe.
    import pipeline
    monkeypatch.setattr(pipeline, "OUTPUTS_DIR", str(tmp_path))

    # Stub update_job so we don't need a DB for this unit test.
    monkeypatch.setattr(pipeline, "update_job", lambda *a, **kw: None)

    # Capture what download_object is called with — that's our proof
    # the derived path makes it through.
    download_calls: list[tuple[str, str]] = []

    def _fake_download(key: str, dest: str) -> bool:
        download_calls.append((key, dest))
        # Write a tiny placeholder so subsequent pipeline steps that
        # check the file exists pass.
        with open(dest, "wb") as f:
            f.write(b"FAKE WAV")
        return True

    monkeypatch.setattr(pipeline.storage, "download_object", _fake_download)

    # Stop the pipeline before it tries to actually do anything heavy
    # (Whisper, Gemini, etc). run_pipeline catches all exceptions and
    # flips the row to error — so we can't rely on pytest.raises; we
    # just verify the side-effect we care about (download_object got
    # called with the derived path).
    def _stop(*a, **kw):
        raise RuntimeError("stop after path derivation")

    monkeypatch.setattr(pipeline, "transcribe", _stop)
    monkeypatch.setattr(pipeline, "_transcribe_via_openai_api", _stop)

    job_id = "retrytest123"
    r2_key = "inputs/default/retrytest123/Mi_audio.wav"

    # run_pipeline catches the RuntimeError internally and returns
    # without raising. That's fine — we only care that download
    # happened with the right path BEFORE the deliberate failure.
    pipeline.run_pipeline(
        job_id=job_id,
        mp3_path=None,           # the retry path passes None
        artist="Test",
        style="oscuro",
        input_r2_key=r2_key,
    )

    # The download must have been called with a DERIVED path, not None.
    assert download_calls, (
        "storage.download_object was never called — pipeline likely "
        "crashed before reaching the R2 fetch"
    )
    actual_dest = download_calls[0][1]
    assert actual_dest is not None
    assert os.path.basename(actual_dest) == "Mi_audio.wav", (
        f"expected derived basename 'Mi_audio.wav', got {actual_dest!r}"
    )
    assert os.path.dirname(actual_dest).endswith(job_id), (
        f"derived path should live under job_dir/{job_id}, got {actual_dest!r}"
    )


def test_run_pipeline_no_longer_crashes_on_none_mp3_path(tmp_path, monkeypatch):
    """Regression test for the exact TypeError from the incident:
        TypeError: stat: path should be string, bytes, os.PathLike or
        integer, not NoneType

    Verifies that with mp3_path=None + input_r2_key set, the pipeline
    does NOT raise that TypeError. (It will still bail out later in
    the pipeline because we stub out the heavy work — that's fine,
    the test only cares about the path-derivation block surviving.)
    """
    import pipeline
    monkeypatch.setattr(pipeline, "OUTPUTS_DIR", str(tmp_path))
    monkeypatch.setattr(pipeline, "update_job", lambda *a, **kw: None)
    monkeypatch.setattr(
        pipeline.storage, "download_object",
        lambda key, dest: (open(dest, "wb").write(b"x"), True)[1],
    )

    class _StopEarly(Exception):
        pass

    monkeypatch.setattr(pipeline, "transcribe", lambda *a, **k: (_ for _ in ()).throw(_StopEarly()))
    monkeypatch.setattr(pipeline, "_transcribe_via_openai_api", lambda *a, **k: (_ for _ in ()).throw(_StopEarly()))

    # The only failure mode we care about: TypeError on None. Anything
    # else (our deliberate _StopEarly, AttributeError on stubbed code,
    # etc.) is fine for this test.
    try:
        pipeline.run_pipeline(
            job_id="nofail456",
            mp3_path=None,
            artist="Test",
            style="oscuro",
            input_r2_key="inputs/x/y/track.wav",
        )
    except TypeError as e:
        if "stat" in str(e).lower() and "NoneType" in str(e):
            pytest.fail(f"the original incident TypeError came back: {e}")
        # Other TypeErrors are unrelated and not what this test
        # guards against — re-raise so we don't hide real bugs.
        raise
    except _StopEarly:
        pass  # got past the path-derivation block, that's what we wanted
    except Exception:
        # Anything else from the stubbed downstream is fine. The path
        # block survived, which is the contract.
        pass
