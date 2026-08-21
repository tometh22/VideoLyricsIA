"""Content-addressed cache primitives for transcription quality artifacts.

The quality worker produces expensive intermediate artifacts that are safe to
reuse only when *all* of their inputs match.  This module deliberately keeps
the cache independent from the transcription pipeline so it can be introduced
and benchmarked before any write path is enabled.

An address includes the audio digest plus the model, configuration, release,
and evidence lineage.  Values are compressed, integrity checked, and stored
with both an atomic Redis TTL and an expiry timestamp inside the envelope.
Redis failures are fail-open cache misses: quality computation remains correct
when the cache is unavailable.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import struct
import time
import zlib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping

logger = logging.getLogger("genly.quality_cache")


class ArtifactKind(str, Enum):
    FEATURES = "features"
    BOUNDARIES = "boundaries"
    CTC = "ctc"
    N_BEST = "n_best"


_KINDS = frozenset(kind.value for kind in ArtifactKind)
_AUDIO_HASH_RE = re.compile(r"^(?:sha256:)?([0-9a-fA-F]{64})$")
_MAGIC = b"GENLYQC1"
_HEADER_LENGTH = struct.Struct(">I")
_MAX_HEADER_BYTES = 64 * 1024
_SCHEMA_VERSION = 1
_NAMESPACE = "genly:quality-cache:v1"

# Defaults intentionally expire intermediates sooner than source audio.  They
# can be tuned independently without changing an address; a TTL controls
# retention, never semantic identity.
_DEFAULT_TTL_SECONDS = {
    ArtifactKind.FEATURES.value: 7 * 24 * 3600,
    ArtifactKind.BOUNDARIES.value: 7 * 24 * 3600,
    ArtifactKind.CTC.value: 3 * 24 * 3600,
    ArtifactKind.N_BEST.value: 2 * 24 * 3600,
}
_TTL_ENV = {
    ArtifactKind.FEATURES.value: "QUALITY_CACHE_FEATURES_TTL_SECONDS",
    ArtifactKind.BOUNDARIES.value: "QUALITY_CACHE_BOUNDARIES_TTL_SECONDS",
    ArtifactKind.CTC.value: "QUALITY_CACHE_CTC_TTL_SECONDS",
    ArtifactKind.N_BEST.value: "QUALITY_CACHE_N_BEST_TTL_SECONDS",
}
_DEFAULT_MAX_TTL_SECONDS = 30 * 24 * 3600
_DEFAULT_MAX_VALUE_BYTES = 64 * 1024 * 1024
_DEFAULT_MAX_STORED_BYTES = 8 * 1024 * 1024


def _canonical_json(value: Any) -> bytes:
    """Stable JSON used by both addresses and JSON artifacts."""
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def sha256_file(path: str | os.PathLike[str], chunk_size: int = 1024 * 1024) -> str:
    """Return the full SHA-256 of an audio file without loading it in memory."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class QualityCacheAddress:
    """Complete semantic identity of one cached quality artifact.

    ``model``, ``config`` and ``lineage`` must be JSON-serializable.  Lineage
    should identify upstream artifacts/models (not request IDs), so equivalent
    work converges on one address while evidence from a different genealogy
    cannot collide.
    """

    artifact: ArtifactKind | str
    audio_hash: str
    model: str | Mapping[str, Any]
    config: Mapping[str, Any]
    release: str
    lineage: Mapping[str, Any]

    def __post_init__(self) -> None:
        kind = self.kind
        if kind not in _KINDS:
            raise ValueError(f"unsupported quality cache artifact: {kind!r}")
        match = _AUDIO_HASH_RE.fullmatch(str(self.audio_hash).strip())
        if not match:
            raise ValueError(
                "audio_hash must be a complete 64-character SHA-256 hex digest"
            )
        if not str(self.release).strip():
            raise ValueError("release must be non-empty")
        if not isinstance(self.model, (str, Mapping)) or not self.model:
            raise ValueError("model must be a non-empty string or mapping")
        if not isinstance(self.config, Mapping):
            raise ValueError("config must be a mapping")
        if not isinstance(self.lineage, Mapping) or not self.lineage:
            raise ValueError("lineage must be non-empty")
        # Fail during address construction instead of much later at Redis I/O.
        _canonical_json(self.identity)

    @property
    def kind(self) -> str:
        if isinstance(self.artifact, ArtifactKind):
            return self.artifact.value
        return str(self.artifact).strip().lower()

    @property
    def normalized_audio_hash(self) -> str:
        match = _AUDIO_HASH_RE.fullmatch(str(self.audio_hash).strip())
        if match is None:  # guarded in __post_init__; keeps typing explicit
            raise ValueError("invalid audio_hash")
        return match.group(1).lower()

    @property
    def identity(self) -> dict[str, Any]:
        return {
            "artifact": self.kind,
            "audio_sha256": self.normalized_audio_hash,
            "config": self.config,
            "lineage": self.lineage,
            "model": self.model,
            "release": str(self.release).strip(),
            "schema": _SCHEMA_VERSION,
        }

    @property
    def digest(self) -> str:
        return hashlib.sha256(_canonical_json(self.identity)).hexdigest()

    @property
    def redis_key(self) -> str:
        return f"{_NAMESPACE}:{self.kind}:{self.digest}"


def _default_redis():
    """Use a dedicated cache Redis; never evict durable RQ job metadata."""
    url = os.environ.get("QUALITY_CACHE_REDIS_URL", "").strip()
    if not url:
        return None
    try:
        from redis import Redis
        return Redis.from_url(
            url, socket_connect_timeout=2, socket_timeout=3,
            health_check_interval=30,
        )
    except Exception:
        return None


def _default_metric(name: str, amount: int = 1) -> None:
    try:
        from ops_metrics import increment

        increment(name, amount)
    except Exception:
        # Observability must never make an otherwise valid cache read fail.
        pass


def _positive_env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("[QUALITY_CACHE] invalid %s=%r; using %d", name, raw, default)
        return default
    if value <= 0:
        logger.warning("[QUALITY_CACHE] non-positive %s=%r; using %d", name, raw, default)
        return default
    return value


class QualityCache:
    """Best-effort Redis cache with content integrity and bounded retention."""

    def __init__(
        self,
        redis_client: Any | None = None,
        *,
        clock: Callable[[], float] = time.time,
        metric_sink: Callable[[str, int], None] = _default_metric,
    ) -> None:
        self._redis_client = redis_client
        self._redis_resolved = redis_client is not None
        self._clock = clock
        self._metric_sink = metric_sink

    def _redis(self):
        if not self._redis_resolved:
            self._redis_client = _default_redis()
            # Retry discovery after a startup/network blip instead of turning a
            # long-lived worker into a permanent cache bypass. Once a client
            # exists, redis-py's pool owns reconnects for that client.
            self._redis_resolved = self._redis_client is not None
        return self._redis_client

    def _metric(self, address: QualityCacheAddress, outcome: str, amount: int = 1) -> None:
        try:
            self._metric_sink(
                f"quality_cache.{address.kind}.{outcome}", int(amount)
            )
        except Exception:
            pass

    def _limits(self) -> tuple[int, int]:
        max_value = _positive_env_int(
            "QUALITY_CACHE_MAX_VALUE_BYTES", _DEFAULT_MAX_VALUE_BYTES
        )
        max_stored = _positive_env_int(
            "QUALITY_CACHE_MAX_STORED_BYTES", _DEFAULT_MAX_STORED_BYTES
        )
        return max_value, max_stored

    def ttl_seconds(self, address: QualityCacheAddress, requested: int | None = None) -> int:
        """Resolve a positive, globally capped TTL for an artifact."""
        if requested is None:
            ttl = _positive_env_int(
                _TTL_ENV[address.kind], _DEFAULT_TTL_SECONDS[address.kind]
            )
        else:
            if isinstance(requested, bool):
                raise ValueError("ttl_s must be a positive integer")
            try:
                ttl = int(requested)
            except (TypeError, ValueError) as exc:
                raise ValueError("ttl_s must be a positive integer") from exc
            if ttl <= 0:
                raise ValueError("ttl_s must be a positive integer")
        cap = _positive_env_int(
            "QUALITY_CACHE_MAX_TTL_SECONDS", _DEFAULT_MAX_TTL_SECONDS
        )
        return min(ttl, cap)

    def put_bytes(
        self,
        address: QualityCacheAddress,
        payload: bytes,
        *,
        ttl_s: int | None = None,
        content_type: str = "application/octet-stream",
    ) -> bool:
        """Compress and atomically store bytes with a finite Redis TTL."""
        if not isinstance(payload, bytes):
            raise TypeError("payload must be bytes")
        if not content_type or len(content_type) > 128:
            raise ValueError("content_type must be 1..128 characters")
        max_value, max_stored = self._limits()
        if len(payload) > max_value:
            self._metric(address, "oversize")
            raise ValueError(f"quality cache payload exceeds {max_value} bytes")

        ttl = self.ttl_seconds(address, ttl_s)
        created_at = float(self._clock())
        compressed = zlib.compress(payload, level=6)
        header = _canonical_json(
            {
                "codec": "zlib",
                "content_type": content_type,
                "created_at": created_at,
                "expires_at": created_at + ttl,
                "identity_digest": address.digest,
                "payload_sha256": hashlib.sha256(payload).hexdigest(),
                "schema": _SCHEMA_VERSION,
                "size": len(payload),
            }
        )
        if len(header) > _MAX_HEADER_BYTES:
            raise ValueError("quality cache header is too large")
        frame = _MAGIC + _HEADER_LENGTH.pack(len(header)) + header + compressed
        if len(frame) > max_stored:
            self._metric(address, "oversize")
            raise ValueError(f"compressed quality cache value exceeds {max_stored} bytes")

        client = self._redis()
        if client is None:
            self._metric(address, "write_error")
            return False
        try:
            # SETEX is atomic: no cache value can be written without expiry.
            client.setex(address.redis_key, ttl, frame)
        except Exception as exc:
            self._metric(address, "write_error")
            logger.warning(
                "[QUALITY_CACHE] write failed kind=%s key=%s: %s",
                address.kind,
                address.digest[:12],
                exc,
            )
            return False
        self._metric(address, "write")
        self._metric(address, "write_bytes", len(payload))
        return True

    def put_json(
        self,
        address: QualityCacheAddress,
        value: Any,
        *,
        ttl_s: int | None = None,
    ) -> bool:
        return self.put_bytes(
            address,
            _canonical_json(value),
            ttl_s=ttl_s,
            content_type="application/json",
        )

    def _delete_quietly(self, address: QualityCacheAddress) -> None:
        client = self._redis()
        if client is None:
            return
        try:
            client.delete(address.redis_key)
        except Exception:
            pass

    def _miss(self, address: QualityCacheAddress, reason: str | None = None) -> None:
        self._metric(address, "miss")
        if reason:
            self._metric(address, reason)

    def _read(
        self,
        address: QualityCacheAddress,
        *,
        expected_content_type: str | None = None,
        record_hit: bool = True,
    ) -> bytes | None:
        client = self._redis()
        if client is None:
            self._miss(address, "read_error")
            return None
        try:
            frame = client.get(address.redis_key)
        except Exception as exc:
            self._miss(address, "read_error")
            logger.warning(
                "[QUALITY_CACHE] read failed kind=%s key=%s: %s",
                address.kind,
                address.digest[:12],
                exc,
            )
            return None
        if frame is None:
            self._miss(address)
            return None
        if isinstance(frame, str):
            frame = frame.encode("latin1")

        max_value, max_stored = self._limits()
        try:
            prefix_len = len(_MAGIC) + _HEADER_LENGTH.size
            if len(frame) > max_stored or len(frame) < prefix_len:
                raise ValueError("invalid frame size")
            if frame[: len(_MAGIC)] != _MAGIC:
                raise ValueError("invalid frame magic")
            header_len = _HEADER_LENGTH.unpack(
                frame[len(_MAGIC) : prefix_len]
            )[0]
            if header_len <= 0 or header_len > _MAX_HEADER_BYTES:
                raise ValueError("invalid header size")
            body_at = prefix_len + header_len
            if body_at > len(frame):
                raise ValueError("truncated frame")
            header = json.loads(frame[prefix_len:body_at].decode("utf-8"))
            if header.get("schema") != _SCHEMA_VERSION:
                raise ValueError("unsupported schema")
            if header.get("identity_digest") != address.digest:
                raise ValueError("identity mismatch")
            if header.get("codec") != "zlib":
                raise ValueError("unsupported codec")
            if expected_content_type and header.get("content_type") != expected_content_type:
                raise ValueError("content type mismatch")
            expires_at = float(header["expires_at"])
            if self._clock() >= expires_at:
                self._delete_quietly(address)
                self._miss(address, "expired")
                return None
            size = int(header["size"])
            if size < 0 or size > max_value:
                raise ValueError("invalid payload size")
            inflater = zlib.decompressobj()
            payload = inflater.decompress(frame[body_at:], max_value + 1)
            if inflater.unconsumed_tail or len(payload) > max_value:
                raise ValueError("decompressed payload too large")
            payload += inflater.flush(max_value + 1 - len(payload))
            if inflater.unused_data or not inflater.eof or len(payload) != size:
                raise ValueError("truncated or oversized payload")
            if hashlib.sha256(payload).hexdigest() != header.get("payload_sha256"):
                raise ValueError("payload checksum mismatch")
        except Exception as exc:
            self._delete_quietly(address)
            self._miss(address, "corrupt")
            logger.warning(
                "[QUALITY_CACHE] discarded corrupt value kind=%s key=%s: %s",
                address.kind,
                address.digest[:12],
                exc,
            )
            return None

        if record_hit:
            self._metric(address, "hit")
            self._metric(address, "hit_bytes", len(payload))
        return payload

    def get_bytes(self, address: QualityCacheAddress) -> bytes | None:
        return self._read(address)

    def get_json(self, address: QualityCacheAddress) -> Any | None:
        payload = self._read(
            address,
            expected_content_type="application/json",
            record_hit=False,
        )
        if payload is None:
            return None
        try:
            value = json.loads(payload.decode("utf-8"))
        except Exception as exc:
            # This should be impossible through put_json, but callers may have
            # written bytes under the JSON MIME type. Do not serve ambiguity.
            self._delete_quietly(address)
            self._metric(address, "miss")
            self._metric(address, "deserialize_error")
            logger.warning(
                "[QUALITY_CACHE] invalid JSON kind=%s key=%s: %s",
                address.kind,
                address.digest[:12],
                exc,
            )
            return None
        self._metric(address, "hit")
        self._metric(address, "hit_bytes", len(payload))
        return value

    def delete(self, address: QualityCacheAddress) -> bool:
        client = self._redis()
        if client is None:
            return False
        try:
            return bool(client.delete(address.redis_key))
        except Exception:
            self._metric(address, "delete_error")
            return False
