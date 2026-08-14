"""Defense-in-depth tests for the WhisperX cache audio_hash recheck.

Motivation: cache_key is already content-addressed (key starts with
audio_hash) so a row collision is mathematically improbable (~10^-19
at 16 hex chars). But the chain "lyrics of song B appear when
transcribing song A" is high-impact enough that one cheap column
compare is worth pinning. Catches:
  - manual DB edits that mutate a row's audio_hash without bumping key
  - future migrations that copy segments between cache rows
  - any code path that calls _cache_write with a wrong (key, hash) pair

The tests use a fake SessionLocal so they're hermetic — no real DB.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

# Make backend modules importable.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class FakeRow:
    def __init__(self, cache_key, audio_hash, segments):
        self.cache_key = cache_key
        self.audio_hash = audio_hash
        self.segments = segments


class FakeQuery:
    def __init__(self, rows):
        self.rows = rows
        self._filter_key = None

    def filter(self, expr):
        # The real call is `.filter(TranscriptionCache.cache_key == cache_key)`.
        # We stash the RHS by attribute access on the BinaryExpression — but
        # since the test sets `_pending_key` on the session before each call,
        # we just look that up.
        self._filter_key = self.rows.get("__pending_key")
        return self

    def first(self):
        key = self._filter_key
        if key is None:
            return None
        return self.rows.get(key)


class FakeSession:
    def __init__(self, rows: dict):
        self._rows = rows

    def query(self, _model):
        return FakeQuery(self._rows)

    def close(self):
        pass


@pytest.fixture
def fake_db(monkeypatch):
    """Patch SessionLocal so _cache_lookup runs against an in-memory dict."""
    rows: dict = {}

    class FakeSessionLocal:
        def __call__(self):
            return FakeSession(rows)

    fake = FakeSessionLocal()
    # The fn imports `from database import TranscriptionCache, SessionLocal`
    # inside the try block, so we need a fake `database` module on sys.modules.
    import types
    fake_db_mod = types.ModuleType("database")
    fake_db_mod.SessionLocal = fake
    fake_db_mod.TranscriptionCache = type("TranscriptionCache", (), {
        "cache_key": "cache_key",
    })
    sys.modules["database"] = fake_db_mod
    yield rows
    sys.modules.pop("database", None)


def test_cache_hit_with_matching_audio_hash_returns_segments(fake_db):
    """Happy path: key matches, audio_hash matches → segments returned."""
    import whisperx_transcribe as wx
    key = "wx:abc123def456789a:es:"
    audio_hash = "abc123def456789a"
    fake_db["__pending_key"] = key
    fake_db[key] = FakeRow(
        cache_key=key,
        audio_hash=audio_hash,
        segments=json.dumps([{"start": 0, "end": 1, "text": "hola"}]),
    )
    out = wx._cache_lookup(key, expected_audio_hash=audio_hash)
    assert out is not None
    assert out[0]["text"] == "hola"


def test_cache_miss_returns_none(fake_db):
    """No row for this key → None, no crash."""
    import whisperx_transcribe as wx
    fake_db["__pending_key"] = "wx:nope:es:"
    assert wx._cache_lookup("wx:nope:es:", expected_audio_hash="nope") is None


def test_audio_hash_mismatch_treats_as_miss(fake_db):
    """The defense-in-depth case: row exists for the key but its
    audio_hash column drifted from what we expect. Must return None
    (force live recompute) so the wrong segments never reach the user."""
    import whisperx_transcribe as wx
    key = "wx:abc123def456789a:es:"
    fake_db["__pending_key"] = key
    fake_db[key] = FakeRow(
        cache_key=key,
        audio_hash="DRIFTED_HASH",   # mutated by a hypothetical bug
        segments=json.dumps([{"start": 0, "end": 1, "text": "WRONG SONG"}]),
    )
    out = wx._cache_lookup(key, expected_audio_hash="abc123def456789a")
    assert out is None


def test_audio_hash_check_is_opt_in(fake_db):
    """Backward-compat: if no expected_audio_hash is provided, the
    legacy callers (if any) still get the cached segments without
    the cross-check kicking in. This preserves the original behavior
    for any test/script that imports _cache_lookup directly."""
    import whisperx_transcribe as wx
    key = "wx:legacy:es:"
    fake_db["__pending_key"] = key
    fake_db[key] = FakeRow(
        cache_key=key,
        audio_hash="any_hash_at_all",
        segments=json.dumps([{"start": 0, "end": 1, "text": "ok"}]),
    )
    out = wx._cache_lookup(key)   # no expected_audio_hash kwarg
    assert out is not None
    assert out[0]["text"] == "ok"


def test_null_stored_hash_does_not_block_hit(fake_db):
    """If a legacy row has audio_hash=None (pre-cross-check era), we
    accept it. The cross-check only fires when both sides have a value."""
    import whisperx_transcribe as wx
    key = "wx:legacy_null:es:"
    fake_db["__pending_key"] = key
    fake_db[key] = FakeRow(
        cache_key=key,
        audio_hash=None,
        segments=json.dumps([{"start": 0, "end": 1, "text": "ok"}]),
    )
    out = wx._cache_lookup(key, expected_audio_hash="abc")
    assert out is not None
