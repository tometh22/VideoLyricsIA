"""Regression tests for moviepy_utf8_patch.

Reproduces the UMG Chile crash (Sentry PYTHON-FASTAPI-2N): moviepy's
`ffmpeg_parse_infos` did `error.decode('utf8')` in strict mode on ffmpeg's
stderr, so an accented title ("La Vida Al Revés" → byte 0xe9) killed the render
inside AudioFileClip(). These tests exercise the tolerant wrapper WITHOUT a real
moviepy install (it is stubbed in conftest.py), by driving `_ORIGINAL_PARSE_INFOS`
and `_ascii_metadata_free_copy` directly.
"""

import os
import subprocess
import wave

import pytest

import moviepy_utf8_patch as mp


def _runnable_ffmpeg():
    """The real ffmpeg binary the patch would use, or None if unavailable/unrunnable
    (so the container-remux tests self-skip in a bare CI without ffmpeg)."""
    try:
        binary = mp._ffmpeg_binary()
        subprocess.run([binary, "-version"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return binary
    except Exception:
        return None


_FFMPEG = _runnable_ffmpeg()


def _write_pcm_wav(path):
    """Write a tiny but valid PCM/WAVE file (1s of silence, 16-bit mono)."""
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 8000)


def _unicode_error():
    # The exact shape of the production failure.
    return UnicodeDecodeError("utf-8", b"\xe9", 1460, 1461, "invalid continuation byte")


def test_happy_path_does_not_remux(monkeypatch):
    """ASCII files: the original parser succeeds and no fallback copy is made."""
    remuxed = []
    monkeypatch.setattr(mp, "_ORIGINAL_PARSE_INFOS",
                        lambda fn, *a, **k: {"duration": 3.0, "fn": fn})
    monkeypatch.setattr(mp, "_ascii_metadata_free_copy",
                        lambda src: remuxed.append(src) or "/should/not/happen")

    out = mp._tolerant_parse_infos("/out/plain-name.wav")

    assert out == {"duration": 3.0, "fn": "/out/plain-name.wav"}
    assert remuxed == []  # no work on the happy path


def test_retries_on_unicode_error_then_cleans_up(tmp_path, monkeypatch):
    """Accented file → original raises → retry against an ASCII copy, then the
    temp copy is deleted. The caller-visible result comes from the retry."""
    safe = tmp_path / "safe.wav"
    safe.write_bytes(b"x")

    seen = []

    def fake_original(fn, *a, **k):
        seen.append(fn)
        if fn.endswith("Revés.wav"):
            raise _unicode_error()
        return {"duration": 3.0, "fn": fn}

    monkeypatch.setattr(mp, "_ORIGINAL_PARSE_INFOS", fake_original)
    monkeypatch.setattr(mp, "_ascii_metadata_free_copy", lambda src: str(safe))

    out = mp._tolerant_parse_infos("/out/03-Los Tres-La Vida Al Revés.wav")

    assert out == {"duration": 3.0, "fn": str(safe)}
    assert seen == ["/out/03-Los Tres-La Vida Al Revés.wav", str(safe)]
    assert not safe.exists()  # temp copy cleaned up even though parse succeeded


def test_cleanup_runs_even_if_retry_also_raises(tmp_path, monkeypatch):
    """If the retry itself fails, we still remove the temp copy and propagate."""
    safe = tmp_path / "safe.wav"
    safe.write_bytes(b"x")

    def always_bad(fn, *a, **k):
        raise _unicode_error()

    monkeypatch.setattr(mp, "_ORIGINAL_PARSE_INFOS", always_bad)
    monkeypatch.setattr(mp, "_ascii_metadata_free_copy", lambda src: str(safe))

    try:
        mp._tolerant_parse_infos("/out/Corazón.wav")
        raised = False
    except UnicodeDecodeError:
        raised = True

    assert raised
    assert not safe.exists()  # no temp leak on the double-failure path


@pytest.mark.skipif(_FFMPEG is None, reason="ffmpeg binary not available")
def test_metadata_free_copy_survives_wav_bytes_named_mp3(tmp_path):
    """UMG Chile edit incident (2026-07-09, Sentry PYTHON-FASTAPI-2P).

    run_edit_pipeline saves the input as `source_audio.mp3` regardless of the real
    codec, so a WAV upload is PCM bytes in an .mp3-named file. The old fallback
    inferred the temp container from that lying extension and did `-c copy` into an
    .mp3 container → ffmpeg exit 234 → CalledProcessError killed the whole edit.
    The fixed .mkv target must remux it cleanly."""
    src = tmp_path / "source_audio.mp3"   # name lies: the bytes are PCM/WAV
    _write_pcm_wav(str(src))

    # Anchor the reproduction: the pre-fix shape (PCM `-c copy` into an .mp3
    # container) really does fail with a non-zero exit.
    bad = tmp_path / "bad.mp3"
    rc = subprocess.run(
        [_FFMPEG, "-y", "-i", str(src), "-map_metadata", "-1", "-c", "copy", str(bad)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode
    assert rc != 0, "expected the pre-fix .mp3-container remux to fail (exit 234)"

    # The fix: a universal container remux succeeds and yields a real, non-empty file.
    safe = mp._ascii_metadata_free_copy(str(src))
    try:
        assert safe.endswith(".mkv")
        assert os.path.getsize(safe) > 0
    finally:
        try:
            os.remove(safe)
        except OSError:
            pass


@pytest.mark.skipif(_FFMPEG is None, reason="ffmpeg binary not available")
def test_metadata_free_copy_reraises_calledprocesserror_on_bad_input(tmp_path):
    """A genuinely unreadable input must surface a CalledProcessError (with ffmpeg's
    stderr attached), NOT get masked — so the edit error is classified as `render`
    and stays diagnosable. The temp file must not leak."""
    src = tmp_path / "source_audio.mp3"
    src.write_bytes(b"this is not audio")

    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        mp._ascii_metadata_free_copy(str(src))
    # stderr must be captured (not DEVNULL'd) so the failure stays diagnosable and
    # error_taxonomy still classifies the ffmpeg failure as `render`.
    assert excinfo.value.stderr is not None


def test_apply_patch_never_raises_and_is_idempotent():
    """Importing this module must never break `import pipeline`, regardless of
    whether the real moviepy is present (CI stubs it). apply_patch() returns a
    bool (True=active, False=skipped) and is safe to call repeatedly."""
    result = mp.apply_patch()
    assert isinstance(result, bool)
    assert mp.apply_patch() == result  # idempotent, no re-patching or errors
