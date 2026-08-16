"""Hermetic tests for the transcription-quality content-addressed cache."""

from __future__ import annotations

import hashlib
import os
import sys
from collections import Counter

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from quality_cache import (  # noqa: E402
    ArtifactKind,
    QualityCache,
    QualityCacheAddress,
    sha256_file,
)


AUDIO_HASH = hashlib.sha256(b"same-audio").hexdigest()


class FakeRedis:
    def __init__(self):
        self.store: dict[str, bytes] = {}
        self.setex_calls: list[tuple[str, int, bytes]] = []
        self.deleted: list[str] = []

    def setex(self, key, ttl, value):
        self.setex_calls.append((key, ttl, value))
        self.store[key] = value

    def get(self, key):
        return self.store.get(key)

    def delete(self, key):
        self.deleted.append(key)
        return int(self.store.pop(key, None) is not None)


class BrokenRedis:
    def get(self, _key):
        raise ConnectionError("redis down")

    def setex(self, _key, _ttl, _value):
        raise ConnectionError("redis down")


def address(kind=ArtifactKind.FEATURES, **overrides):
    values = {
        "artifact": kind,
        "audio_hash": AUDIO_HASH,
        "model": {"name": "wav2vec2", "revision": "model-sha"},
        "config": {"hop_ms": 10, "sample_rate": 16000},
        "release": "backend-release-sha",
        "lineage": {"stem": "demucs:abc", "mix": AUDIO_HASH},
    }
    values.update(overrides)
    return QualityCacheAddress(**values)


@pytest.fixture
def harness():
    redis = FakeRedis()
    metrics = Counter()
    now = [1_000.0]

    def metric(name, amount=1):
        metrics[name] += amount

    cache = QualityCache(redis, clock=lambda: now[0], metric_sink=metric)
    return cache, redis, metrics, now


def test_address_is_canonical_and_every_semantic_dimension_invalidates():
    first = address(
        config={"sample_rate": 16000, "hop_ms": 10},
        lineage={"mix": AUDIO_HASH, "stem": "demucs:abc"},
    )
    reordered = address(
        config={"hop_ms": 10, "sample_rate": 16000},
        lineage={"stem": "demucs:abc", "mix": AUDIO_HASH},
    )
    assert first.digest == reordered.digest
    assert first.redis_key == reordered.redis_key

    variants = [
        address(audio_hash=hashlib.sha256(b"other").hexdigest()),
        address(model={"name": "wav2vec2", "revision": "other"}),
        address(config={"hop_ms": 20, "sample_rate": 16000}),
        address(release="other-release"),
        address(lineage={"stem": "demucs:different"}),
        address(kind=ArtifactKind.CTC),
    ]
    assert all(candidate.digest != first.digest for candidate in variants)
    assert first.redis_key.startswith("genly:quality-cache:v1:features:")


def test_address_rejects_incomplete_or_noncanonical_identity():
    with pytest.raises(ValueError, match="audio_hash"):
        address(audio_hash="job-42")
    with pytest.raises(ValueError, match="audio_hash"):
        address(audio_hash=AUDIO_HASH[:16])
    with pytest.raises(ValueError, match="release"):
        address(release="")
    with pytest.raises(ValueError, match="lineage"):
        address(lineage={})
    with pytest.raises(ValueError, match="config"):
        address(config="hop=10")
    with pytest.raises(ValueError, match="model"):
        address(model="")
    with pytest.raises(ValueError, match="unsupported"):
        address(kind="video")
    with pytest.raises(ValueError):
        address(config={"bad": float("nan")})


def test_sha256_file_streams_full_digest(tmp_path):
    audio = tmp_path / "audio.wav"
    audio.write_bytes((b"audio-data" * 1000) + b"tail")
    assert sha256_file(audio, chunk_size=17) == hashlib.sha256(
        audio.read_bytes()
    ).hexdigest()


@pytest.mark.parametrize("kind", list(ArtifactKind))
def test_json_round_trip_and_hit_miss_metrics_for_every_artifact(harness, kind):
    cache, redis, metrics, _now = harness
    key = address(kind)
    value = {"events": [{"start": 60.85, "end": 63.77}], "rank": 1}

    assert cache.get_json(key) is None
    assert cache.put_json(key, value, ttl_s=600) is True
    assert cache.get_json(key) == value

    prefix = f"quality_cache.{key.kind}"
    assert metrics[f"{prefix}.miss"] == 1
    assert metrics[f"{prefix}.hit"] == 1
    assert metrics[f"{prefix}.write"] == 1
    assert metrics[f"{prefix}.write_bytes"] > 0
    assert metrics[f"{prefix}.hit_bytes"] > 0
    assert redis.setex_calls[0][1] == 600


def test_binary_ctc_round_trip(harness):
    cache, _redis, metrics, _now = harness
    key = address(ArtifactKind.CTC)
    emissions = bytes(range(256)) * 50

    assert cache.put_bytes(key, emissions, ttl_s=120) is True
    assert cache.get_bytes(key) == emissions
    assert metrics["quality_cache.ctc.hit"] == 1


def test_expiry_is_checked_inside_envelope_and_deleted(harness):
    cache, redis, metrics, now = harness
    key = address(ArtifactKind.BOUNDARIES)
    cache.put_json(key, {"boundaries": [1.0]}, ttl_s=10)

    # FakeRedis intentionally retains values past TTL. The logical timestamp
    # is the second safety net and must prevent serving stale evidence.
    now[0] = 1_010.0
    assert cache.get_json(key) is None
    assert key.redis_key not in redis.store
    assert key.redis_key in redis.deleted
    assert metrics["quality_cache.boundaries.expired"] == 1
    assert metrics["quality_cache.boundaries.miss"] == 1
    assert metrics["quality_cache.boundaries.hit"] == 0


def test_ttl_is_atomic_finite_and_capped(harness, monkeypatch):
    cache, redis, _metrics, _now = harness
    key = address()
    monkeypatch.setenv("QUALITY_CACHE_MAX_TTL_SECONDS", "100")

    cache.put_bytes(key, b"features", ttl_s=999)
    assert redis.setex_calls[-1][1] == 100
    with pytest.raises(ValueError, match="positive"):
        cache.put_bytes(key, b"features", ttl_s=0)


def test_default_ttl_can_be_tuned_per_artifact(harness, monkeypatch):
    cache, redis, _metrics, _now = harness
    monkeypatch.setenv("QUALITY_CACHE_CTC_TTL_SECONDS", "321")
    cache.put_bytes(address(ArtifactKind.CTC), b"ctc")
    assert redis.setex_calls[-1][1] == 321


def test_corruption_is_never_served_and_is_evicted(harness):
    cache, redis, metrics, _now = harness
    key = address(ArtifactKind.N_BEST)
    cache.put_json(key, {"hypotheses": ["real", "no"]})
    frame = bytearray(redis.store[key.redis_key])
    frame[-1] ^= 0xFF
    redis.store[key.redis_key] = bytes(frame)

    assert cache.get_json(key) is None
    assert key.redis_key not in redis.store
    assert metrics["quality_cache.n_best.corrupt"] == 1
    assert metrics["quality_cache.n_best.miss"] == 1
    assert metrics["quality_cache.n_best.hit"] == 0


def test_identity_mismatch_is_never_served(harness):
    cache, redis, metrics, _now = harness
    original = address()
    different_release = address(release="next-release")
    cache.put_bytes(original, b"old-features")
    redis.store[different_release.redis_key] = redis.store[original.redis_key]

    assert cache.get_bytes(different_release) is None
    assert metrics["quality_cache.features.corrupt"] == 1


def test_redis_failures_are_safe_misses_and_failed_writes():
    metrics = Counter()
    cache = QualityCache(
        BrokenRedis(),
        metric_sink=lambda name, amount=1: metrics.update({name: amount}),
    )
    key = address()

    assert cache.get_bytes(key) is None
    assert cache.put_bytes(key, b"result") is False
    assert metrics["quality_cache.features.miss"] == 1
    assert metrics["quality_cache.features.read_error"] == 1
    assert metrics["quality_cache.features.write_error"] == 1


def test_payload_limits_prevent_unbounded_redis_values(harness, monkeypatch):
    cache, redis, metrics, _now = harness
    key = address()
    monkeypatch.setenv("QUALITY_CACHE_MAX_VALUE_BYTES", "4")

    with pytest.raises(ValueError, match="exceeds"):
        cache.put_bytes(key, b"12345")
    assert redis.setex_calls == []
    assert metrics["quality_cache.features.oversize"] == 1
