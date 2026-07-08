"""Regression tests for moviepy_utf8_patch.

Reproduces the UMG Chile crash (Sentry PYTHON-FASTAPI-2N): moviepy's
`ffmpeg_parse_infos` did `error.decode('utf8')` in strict mode on ffmpeg's
stderr, so an accented title ("La Vida Al Revés" → byte 0xe9) killed the render
inside AudioFileClip(). These tests exercise the tolerant wrapper WITHOUT a real
moviepy install (it is stubbed in conftest.py), by driving `_ORIGINAL_PARSE_INFOS`
and `_ascii_metadata_free_copy` directly.
"""

import moviepy_utf8_patch as mp


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


def test_apply_patch_never_raises_and_is_idempotent():
    """Importing this module must never break `import pipeline`, regardless of
    whether the real moviepy is present (CI stubs it). apply_patch() returns a
    bool (True=active, False=skipped) and is safe to call repeatedly."""
    result = mp.apply_patch()
    assert isinstance(result, bool)
    assert mp.apply_patch() == result  # idempotent, no re-patching or errors
