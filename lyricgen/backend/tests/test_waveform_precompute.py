"""Tests for waveform_compute.compute_and_cache_waveform.

PR feat/waveform-precompute 2026-05-27: extracted from main.get_waveform
so the render pipeline can pre-compute and cache the envelope when the
job hits done/pending_review. The on-demand endpoint and the pipeline
share the same helper, so the cache key + payload shape stay in sync.
"""
from unittest.mock import patch

import pytest

from waveform_compute import cache_key_for_job, compute_and_cache_waveform


def test_cache_key_is_canonical():
    """Cache key shape must be stable — endpoint + pipeline both rely
    on it; changing it would silently orphan every cached envelope."""
    assert cache_key_for_job("abc123") == "waveform/abc123.json"


def test_returns_none_when_job_id_empty():
    assert compute_and_cache_waveform("", "inputs/foo.mp3") is None


def test_returns_none_when_input_r2_key_empty():
    assert compute_and_cache_waveform("abc", "") is None


def test_returns_none_when_storage_disabled():
    """In dev environments without R2 configured, the helper must noop
    instead of throwing — the operator already saw it work locally."""
    with patch("storage.is_enabled", return_value=False):
        assert compute_and_cache_waveform("abc", "inputs/foo.mp3") is None


def test_cache_hit_short_circuit():
    """When the cache already has a precomputed envelope, the helper
    returns it WITHOUT downloading the source or running librosa."""
    cached = {"peaks": [0.5, 0.7, 0.3], "duration": 12.5}

    def fake_download(key, path):
        # The helper passes a tempfile path; write the cached JSON there.
        import json
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cached, f)
        return True

    with patch("storage.is_enabled", return_value=True), \
         patch("storage.object_exists", return_value=True) as oe, \
         patch("storage.download_object", side_effect=fake_download) as dl:
        result = compute_and_cache_waveform("abc", "inputs/abc.mp3")

    assert result == cached
    # download_object called ONCE — for the cache, not the source MP3.
    assert dl.call_count == 1
    assert oe.call_count == 1


def test_force_skips_cache_hit():
    """force=True bypasses the cache hit branch even if the cache exists.
    Used by ops rebuild scripts."""
    captured = []

    def fake_download(key, path):
        captured.append(key)
        # Pretend the source download failed → returns None
        return False

    with patch("storage.is_enabled", return_value=True), \
         patch("storage.object_exists", return_value=True), \
         patch("storage.download_object", side_effect=fake_download):
        result = compute_and_cache_waveform("abc", "inputs/abc.mp3", force=True)

    # download_object should have been called with the SOURCE key, not
    # the cache key, because force=True skipped the cache branch.
    assert "inputs/abc.mp3" in captured
    # And it returned None because the (mocked) source download failed.
    assert result is None


def test_source_download_failure_returns_none():
    """If the source MP3 can't be downloaded from R2, the helper returns
    None (so the caller — pipeline or endpoint — can surface the right
    user-facing error)."""
    with patch("storage.is_enabled", return_value=True), \
         patch("storage.object_exists", return_value=False), \
         patch("storage.download_object", return_value=False):
        result = compute_and_cache_waveform("abc", "inputs/abc.mp3")
    assert result is None


def test_compute_path_calls_librosa_and_uploads_cache():
    """Cache miss → download source, run librosa, build envelope, upload
    to cache. Returns the payload."""
    import numpy as np

    def fake_download(key, path):
        # Source download: write a tiny fake audio file.
        with open(path, "wb") as f:
            f.write(b"\x00" * 100)
        return True

    fake_y = np.array([0.1, 0.5, 0.2, 0.9, 0.4], dtype=np.float32)
    fake_sr = 8000

    upload_captured = {}
    def fake_upload(key, body, content_type):
        upload_captured["key"] = key
        upload_captured["body"] = body
        upload_captured["content_type"] = content_type

    with patch("storage.is_enabled", return_value=True), \
         patch("storage.object_exists", return_value=False), \
         patch("storage.download_object", side_effect=fake_download), \
         patch("storage.put_object_bytes", side_effect=fake_upload), \
         patch("librosa.load", return_value=(fake_y, fake_sr)):
        result = compute_and_cache_waveform("abc", "inputs/abc.mp3")

    assert result is not None
    assert "peaks" in result
    assert "duration" in result
    assert isinstance(result["peaks"], list)
    assert len(result["peaks"]) == 1000  # _N_BUCKETS
    # duration = len(y) / sr = 5 / 8000
    assert result["duration"] == pytest.approx(5 / 8000, abs=1e-3)
    # Cache write fired with the canonical key + JSON body.
    assert upload_captured["key"] == "waveform/abc.json"
    assert upload_captured["content_type"] == "application/json"
    import json
    assert json.loads(upload_captured["body"].decode("utf-8")) == result


def test_cache_write_failure_does_not_drop_payload():
    """If upload-to-cache fails (R2 hiccup), the helper still returns
    the computed payload — the next caller will recompute (correct
    behavior, not data loss)."""
    import numpy as np

    def fake_download(key, path):
        with open(path, "wb") as f:
            f.write(b"\x00" * 100)
        return True

    fake_y = np.array([0.5] * 100, dtype=np.float32)
    fake_sr = 8000

    def boom_upload(*args, **kwargs):
        raise RuntimeError("R2 transient failure")

    with patch("storage.is_enabled", return_value=True), \
         patch("storage.object_exists", return_value=False), \
         patch("storage.download_object", side_effect=fake_download), \
         patch("storage.put_object_bytes", side_effect=boom_upload), \
         patch("librosa.load", return_value=(fake_y, fake_sr)):
        result = compute_and_cache_waveform("abc", "inputs/abc.mp3")

    assert result is not None
    assert "peaks" in result


def test_librosa_failure_returns_none():
    """If librosa.load raises (malformed audio, OOM), the helper returns
    None instead of propagating — caller decides what to surface."""
    def fake_download(key, path):
        with open(path, "wb") as f:
            f.write(b"\x00" * 100)
        return True

    with patch("storage.is_enabled", return_value=True), \
         patch("storage.object_exists", return_value=False), \
         patch("storage.download_object", side_effect=fake_download), \
         patch("librosa.load", side_effect=RuntimeError("malformed audio")):
        result = compute_and_cache_waveform("abc", "inputs/abc.mp3")
    assert result is None
