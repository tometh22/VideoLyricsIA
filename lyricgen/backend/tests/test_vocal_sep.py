"""Unit tests for vocal_sep — the pure / gating parts (no Replicate network)."""

import os
import sys
import types
from unittest.mock import MagicMock

import vocal_sep as vs


def _fake_replicate(monkeypatch, *, run):
    """Inject a stand-in `replicate` module (the SDK isn't installed in the
    test venv; prod has it). `run` is the callable / mock for replicate.run."""
    fake = types.ModuleType("replicate")
    fake.run = run
    monkeypatch.setitem(sys.modules, "replicate", fake)


def test_is_enabled_requires_flag_and_token(monkeypatch):
    monkeypatch.delenv("VOCAL_SEP_ENABLED", raising=False)
    monkeypatch.delenv("REPLICATE_API_TOKEN", raising=False)
    assert vs.is_enabled() is False
    monkeypatch.setenv("VOCAL_SEP_ENABLED", "1")
    assert vs.is_enabled() is False           # flag on, no token
    monkeypatch.setenv("REPLICATE_API_TOKEN", "r8_x")
    assert vs.is_enabled() is True
    monkeypatch.setenv("VOCAL_SEP_ENABLED", "0")
    assert vs.is_enabled() is False           # token, flag off


def test_pick_vocals_from_dict():
    assert vs._pick_vocals({"vocals": "u://v", "drums": "u://d"}) == "u://v"
    assert vs._pick_vocals({"voice": "u://v"}) == "u://v"


def test_pick_vocals_missing_returns_none():
    assert vs._pick_vocals({"drums": "u://d", "bass": "u://b"}) is None
    assert vs._pick_vocals(None) is None


def test_pick_vocals_bare_output_passthrough():
    # Some forks emit only the vocal stem as a single value.
    assert vs._pick_vocals("u://only-vocals") == "u://only-vocals"


def test_separate_vocals_none_when_disabled(monkeypatch, tmp_path):
    monkeypatch.delenv("VOCAL_SEP_ENABLED", raising=False)
    f = tmp_path / "a.mp3"
    f.write_bytes(b"x")
    assert vs.separate_vocals(str(f)) is None


def test_separate_vocals_none_when_file_missing(monkeypatch):
    monkeypatch.setenv("VOCAL_SEP_ENABLED", "1")
    monkeypatch.setenv("REPLICATE_API_TOKEN", "r8_x")
    assert vs.separate_vocals("/no/such/file.mp3") is None


def test_separate_vocals_downloads_stem_via_read(monkeypatch, tmp_path):
    monkeypatch.setenv("VOCAL_SEP_ENABLED", "1")
    monkeypatch.setenv("REPLICATE_API_TOKEN", "r8_x")
    src = tmp_path / "song.mp3"
    src.write_bytes(b"fake-mp3")

    # FileOutput-like object exposing .read()
    fake_vocals = MagicMock()
    fake_vocals.read.return_value = b"WAVDATA" * 100
    _fake_replicate(monkeypatch, run=MagicMock(return_value={"vocals": fake_vocals}))
    # validate_stem (post-PR-279 hardening) would reject this fake 700-byte
    # blob — bypass it so this test stays focused on the download/read path.
    # The validator itself has its own test file (test_validate_stem.py).
    monkeypatch.setattr(vs, "validate_stem", lambda p: (True, "ok"))
    out = vs.separate_vocals(str(src))
    assert out is not None
    assert os.path.exists(out)
    assert os.path.getsize(out) > 0
    os.unlink(out)


def test_separate_vocals_none_on_replicate_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("VOCAL_SEP_ENABLED", "1")
    monkeypatch.setenv("REPLICATE_API_TOKEN", "r8_x")
    src = tmp_path / "song.mp3"
    src.write_bytes(b"fake")
    _fake_replicate(monkeypatch, run=MagicMock(side_effect=RuntimeError("boom")))
    assert vs.separate_vocals(str(src)) is None
