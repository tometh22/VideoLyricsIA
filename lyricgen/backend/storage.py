"""Cloud object storage (Cloudflare R2, S3-compatible).

Moves rendered masters out of the local disk so (a) /download redirects to a
signed URL on R2 instead of streaming a 5 GB .mov through uvicorn, and (b) the
local outputs/ directory doesn't grow unbounded.

All helpers are no-ops when R2_* env vars are missing, so local dev still
works without cloud storage. Operators following the docker-compose file
typically set S3_* env vars instead — those are accepted as fallbacks so
the same compose file works for both R2 and any S3-compatible backend.
"""

import logging
import os
import re
import threading
import time
from typing import Optional

logger = logging.getLogger("genly.storage")


def _env(*names: str) -> str:
    """Return the first non-empty value among the given env var names."""
    for n in names:
        v = os.environ.get(n, "").strip()
        if v:
            return v
    return ""


# Accept either R2_* (legacy) or S3_* (docker-compose) — the names diverged
# historically and operators following the compose file ended up with
# is_enabled()==False and silent disk fallback, masking storage breakage.
R2_ACCESS_KEY_ID = _env("R2_ACCESS_KEY_ID", "S3_ACCESS_KEY", "S3_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = _env("R2_SECRET_ACCESS_KEY", "S3_SECRET_KEY", "S3_SECRET_ACCESS_KEY")
R2_ENDPOINT_URL = _env("R2_ENDPOINT_URL", "S3_ENDPOINT_URL")
R2_BUCKET = _env("R2_BUCKET", "S3_BUCKET")

try:
    # A normal retention sweep should be visible in logs, not create a
    # Sentry issue. Alert only when the candidate count looks like an
    # abnormal cleanup spike; operators can tune this per environment.
    R2_CLEANUP_SPIKE_THRESHOLD = int(os.environ.get(
        "R2_CLEANUP_SPIKE_THRESHOLD", "100",
    ))
except (TypeError, ValueError):
    R2_CLEANUP_SPIKE_THRESHOLD = 100

_client = None


def is_enabled() -> bool:
    """True when all R2 env vars are present."""
    return bool(R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY and R2_ENDPOINT_URL and R2_BUCKET)


def warmup() -> bool:
    """Force-initialize the boto3 S3 client (no network).

    boto3 lazy-loads service models, regions, and signers the first time
    a client is constructed — that's ~500–1500 ms of CPU on a fresh
    Python process. After a Railway rolling deploy, the FIRST user
    request that signs an R2 URL pays the entire cost in the request
    thread, which is what made the dashboard look stuck for 1–3 seconds
    immediately after a deploy. Calling this from /health on the first
    healthcheck moves that work to the probe path, so user-facing
    requests start warm.

    Returns True iff the client is now ready, False if not configured
    or boto3 failed to load (caller should treat as best-effort).
    """
    if not is_enabled():
        return False
    try:
        return _get_client() is not None
    except Exception:
        return False


def _get_client():
    global _client
    if _client is not None:
        return _client
    if not is_enabled():
        return None
    import boto3
    from botocore.config import Config
    _client = boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT_URL,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        config=Config(
            signature_version="s3v4",
            retries={"max_attempts": 5, "mode": "adaptive"},
            connect_timeout=30,
            read_timeout=120,
            # The deliveries portal listing fan-outs HEAD calls across a
            # ThreadPoolExecutor(max_workers=16) (main.py portal_get_items).
            # boto3's default urllib3 pool is 10 connections per host, so
            # 16 workers caused "Connection pool is full, discarding
            # connection" spam in production logs and stalled requests
            # while urllib3 churned. 32 gives headroom for that fan-out
            # plus the parallel multipart upload thread pool (20 workers
            # per _transfer_config) without thrashing.
            max_pool_connections=32,
        ),
    )
    return _client


# Separate boto3 client dedicated to /health probes. Has its OWN urllib3
# pool (2 connections) and aggressive timeouts so a probe can:
#   1. Detect the main client's pool saturation — when the main pool is
#      stuck, a probe on the shared client would also hang. This isolated
#      client's HEAD completes (or fails fast) regardless.
#   2. Fail in ≤3 s instead of the 30 s default — Railway's healthcheck
#      probe times out at 5 s, so any check on the hot path must be well
#      under that.
_health_client = None
_health_probe_lock = threading.Lock()
_health_probe_failures = 0
_health_circuit_open_until = 0.0
_health_probe_last_result: tuple[bool, int, str | None] | None = None
_health_probe_last_at = 0.0
_health_probe_executor = None
_health_probe_future = None


def _health_breaker_failure_threshold() -> int:
    try:
        return max(int(os.environ.get("R2_HEALTH_BREAKER_FAILURE_THRESHOLD", "2")), 1)
    except (TypeError, ValueError):
        return 2


def _health_breaker_cooldown_seconds() -> int:
    try:
        return max(int(os.environ.get("R2_HEALTH_BREAKER_COOLDOWN_SECONDS", "30")), 1)
    except (TypeError, ValueError):
        return 30


def _health_probe_cache_seconds() -> int:
    try:
        return max(int(os.environ.get("R2_HEALTH_PROBE_CACHE_SECONDS", "5")), 0)
    except (TypeError, ValueError):
        return 5


def health_probe_state() -> dict:
    """Observable state for the isolated R2 health-probe circuit breaker."""
    remaining = max(0, int(_health_circuit_open_until - time.monotonic()))
    return {
        "state": "open" if remaining else "closed",
        "failures": _health_probe_failures,
        "retry_after_seconds": remaining,
        "probe_inflight": bool(
            _health_probe_future is not None
            and not _health_probe_future.done()
        ),
    }


def _new_health_probe_executor():
    from concurrent.futures import ThreadPoolExecutor

    return ThreadPoolExecutor(max_workers=1, thread_name_prefix="r2-probe")


def _clear_health_probe_execution() -> None:
    """Release a completed probe executor without waiting on its thread."""
    global _health_probe_executor, _health_probe_future
    executor = _health_probe_executor
    _health_probe_executor = None
    _health_probe_future = None
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=True)


def _get_health_client():
    global _health_client
    if _health_client is not None:
        return _health_client
    if not is_enabled():
        return None
    import boto3
    from botocore.config import Config
    _health_client = boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT_URL,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        config=Config(
            signature_version="s3v4",
            retries={"max_attempts": 0},  # /health must fail fast, not retry
            connect_timeout=2,
            read_timeout=3,
            max_pool_connections=2,
        ),
    )
    return _health_client


def probe_r2() -> tuple[bool, int, str | None]:
    """Live R2 reachability check for /health.

    Does a head_bucket against R2_BUCKET via the isolated _health_client.
    Returns (ok, elapsed_ms, error_msg). Total wall-clock acotado por el
    hard cap de 6 s del executor (ver abajo).

    Used by observability.health_snapshot to flag the API as degraded
    when R2 is unreachable or slow (>1.5 s round-trip = pool churn or
    network issue), even when nothing has crashed yet.

    Incidente 2026-06-11: un blip de red dejó las 2 conexiones del pool
    del _health_client colgadas. Como el cliente es un global cacheado,
    cada head_bucket posterior esperaba un slot del pool PARA SIEMPRE →
    /health tardaba >20 s (timeout del que pregunta), el panel mostraba
    "degraded · r2_probe_failed" y los monitores daban falsa alarma —
    con R2 perfectamente sano (un cliente fresco respondía en 200 ms).
    Fix doble: hard cap de wall-clock con un executor, y ante CUALQUIER
    fallo se descarta el cliente cacheado para que el próximo probe
    arranque con pool nuevo (auto-curación en ≤15 s, el período de
    refresh del panel).
    """
    global _health_client, _health_probe_failures, _health_circuit_open_until
    global _health_probe_last_result, _health_probe_last_at
    global _health_probe_executor, _health_probe_future
    now = time.monotonic()
    if now < _health_circuit_open_until:
        remaining = max(1, int(_health_circuit_open_until - now))
        return False, 0, f"circuit_open (retry in {remaining}s)"
    if (
        _health_probe_last_result is not None
        and now - _health_probe_last_at < _health_probe_cache_seconds()
    ):
        return _health_probe_last_result
    # Do not let concurrent /health requests fan out duplicate HEADs during
    # an outage. A caller that loses the lock fails fast; the in-flight probe
    # will update the breaker for the next health snapshot.
    if not _health_probe_lock.acquire(blocking=False):
        return _health_probe_last_result or (False, 0, "probe_inflight")
    from concurrent.futures import TimeoutError as _FutTimeout
    t0 = time.monotonic()
    keep_execution = False
    try:
        # A previous hard-timeout may still be blocked inside urllib3 pool
        # acquisition. Never start a second thread while it remains alive.
        if _health_probe_future is not None:
            if not _health_probe_future.done():
                keep_execution = True
                return _health_probe_last_result or (False, 0, "probe_inflight")
            _clear_health_probe_execution()

        client = _get_health_client()
        if client is None:
            result = (False, 0, "not_configured")
            _health_probe_last_result = result
            _health_probe_last_at = time.monotonic()
            return result
        _health_probe_executor = _new_health_probe_executor()
        _health_probe_future = _health_probe_executor.submit(
            client.head_bucket, Bucket=R2_BUCKET,
        )
        _health_probe_future.result(timeout=6)
        _health_probe_failures = 0
        _health_circuit_open_until = 0.0
        result = (True, int((time.monotonic() - t0) * 1000), None)
        _health_probe_last_result = result
        _health_probe_last_at = time.monotonic()
        return result
    except _FutTimeout:
        # Keep exactly one reference to the stuck execution. Later probes
        # fail fast until it completes; they never accumulate more threads.
        keep_execution = bool(
            _health_probe_future is not None
            and not _health_probe_future.done()
        )
        _health_client = None
        _health_probe_failures += 1
        if _health_probe_failures >= _health_breaker_failure_threshold():
            _health_circuit_open_until = (
                time.monotonic() + _health_breaker_cooldown_seconds()
            )
        result = (
            False,
            int((time.monotonic() - t0) * 1000),
            "probe_timeout (health client reset)",
        )
        _health_probe_last_result = result
        _health_probe_last_at = time.monotonic()
        return result
    except Exception as e:
        _health_client = None
        _health_probe_failures += 1
        if _health_probe_failures >= _health_breaker_failure_threshold():
            _health_circuit_open_until = (
                time.monotonic() + _health_breaker_cooldown_seconds()
            )
        result = (False, int((time.monotonic() - t0) * 1000), str(e)[:120])
        _health_probe_last_result = result
        _health_probe_last_at = time.monotonic()
        return result
    finally:
        if not keep_execution and _health_probe_future is not None:
            _clear_health_probe_execution()
        _health_probe_lock.release()


def _transfer_config():
    """Tuned multipart settings for multi-GB ProRes masters. boto3 defaults
    (8 MB chunks, 10 threads) made our 4.5 GB UMG masters take 25+ min;
    64 MB chunks and 20 threads complete the same payload in 1–3 min on
    Railway egress."""
    from boto3.s3.transfer import TransferConfig
    return TransferConfig(
        multipart_threshold=64 * 1024 * 1024,
        multipart_chunksize=64 * 1024 * 1024,
        max_concurrency=20,
        use_threads=True,
    )


# Keep `_KEY_SAFE` ASCII-only so signed URLs round-trip cleanly through any
# CDN / proxy — and so an attacker can't slip path-traversal segments
# (`..`, `%2f`, NUL, etc.) into a filename to land an object outside their
# tenant prefix and then ask /download to sign that key.
_KEY_SAFE = re.compile(r"[^A-Za-z0-9._-]+")

# This is deliberately explicit and immutable. Bumping the version creates a
# new content-addressed object, so an encoder change can never silently serve
# bytes produced by an older format.
EDITOR_AUDIO_PREVIEW_FORMAT_VERSION = "aac-stereo-96k-v1"


def _safe_filename(filename: str) -> str:
    """Sanitize a user-controlled filename so it is safe to use as the
    last segment of an object key. We:
      - strip path components (basename only),
      - collapse anything that isn't ASCII alnum / dot / underscore / dash,
      - reject leading dots so we can't write "..", "/.hidden", etc.,
      - cap to 200 chars (S3 max key length is 1024; this keeps the
        prefix + filename comfortably under that).
    """
    base = os.path.basename(filename or "")
    cleaned = _KEY_SAFE.sub("_", base).strip(".")
    if not cleaned:
        cleaned = "file"
    return cleaned[:200]


def _safe_key_component(value: str) -> str:
    """Sanitize an identity component without basename semantics.

    Tenant and job identifiers are not filenames. Applying ``basename`` to
    them would make ``tenant/a`` and ``a`` collide at the same object prefix.
    """
    cleaned = _KEY_SAFE.sub("_", str(value or "")).strip(".")
    return (cleaned or "unknown")[:200]


def _object_key(tenant_id: str, job_id: str, filename: str) -> str:
    return f"{_safe_filename(tenant_id)}/{_safe_filename(job_id)}/{_safe_filename(filename)}"


def _input_object_key(tenant_id: str, job_id: str, filename: str) -> str:
    """Inputs (user-uploaded MP3s) live under a separate prefix so lifecycle
    rules can purge them aggressively without touching deliverables."""
    return (
        f"inputs/{_safe_filename(tenant_id)}"
        f"/{_safe_filename(job_id)}/{_safe_filename(filename)}"
    )


def content_addressed_input_key(
    tenant_id: str, job_id: str, audio_sha256: str, filename: str,
) -> str:
    """Immutable source-audio key bound to a validated SHA-256 identity."""
    digest = str(audio_sha256 or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("audio_sha256 must be a lowercase SHA-256 digest")
    return (
        f"inputs/{_safe_key_component(tenant_id)}/{_safe_key_component(job_id)}"
        f"/sha256/{digest}/{_safe_filename(filename)}"
    )


def editor_audio_preview_key(
    audio_sha256: str,
    format_version: str = EDITOR_AUDIO_PREVIEW_FORMAT_VERSION,
) -> str:
    """Return the shared editor-preview key for an immutable audio digest.

    The key intentionally contains no tenant or job identifier: identical
    source bytes share one preview across jobs and tenants. Callers must still
    perform authorization before probing or signing this key; this helper is
    not an authorization boundary.
    """
    digest = str(audio_sha256 or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("audio_sha256 must be a lowercase SHA-256 digest")
    version = str(format_version or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", version):
        raise ValueError("format_version contains invalid characters")
    return f"editor-previews/{digest}/{version}.m4a"


def upload_master(local_path: str, tenant_id: str, job_id: str, filename: str) -> Optional[str]:
    """Upload a rendered file to R2. Returns the object key or None if R2 is
    not configured. Raises on actual S3 errors so the caller can mark the job
    upload_failed."""
    client = _get_client()
    if client is None:
        return None
    key = _object_key(tenant_id, job_id, filename)
    content_type = _guess_content_type(filename)
    extra = {"ContentType": content_type} if content_type else {}
    client.upload_file(
        local_path, R2_BUCKET, key,
        ExtraArgs=extra, Config=_transfer_config(),
    )
    size_mb = os.path.getsize(local_path) / 1024 / 1024
    logger.info("[R2] Uploaded %s (%.1f MB)", key, size_mb)
    return key


def upload_input(local_path: str, tenant_id: str, job_id: str, filename: str) -> Optional[str]:
    """Upload a user-provided input file (MP3, custom background) to R2 so
    that worker containers can fetch it without sharing a filesystem with the
    API. Returns the object key or None if R2 is disabled. Raises on errors."""
    client = _get_client()
    if client is None:
        return None
    key = _input_object_key(tenant_id, job_id, filename)
    content_type = _guess_content_type(filename) or "application/octet-stream"
    client.upload_file(
        local_path, R2_BUCKET, key,
        ExtraArgs={"ContentType": content_type},
        Config=_transfer_config(),
    )
    size_mb = os.path.getsize(local_path) / 1024 / 1024
    logger.info("[R2] Uploaded input %s (%.1f MB)", key, size_mb)
    return key


def object_status(key: str) -> str:
    """Return ``exists``, ``missing`` or ``unavailable`` for an R2 key.

    Lifecycle reconciliation must distinguish a real 404 from a transient
    HEAD failure: deleting a database key on a timeout/403 could orphan a
    valid multi-GB deliverable. Cache callers that only need a bool continue
    to use :func:`object_exists` below.
    """
    client = _get_client()
    if client is None:
        return "unavailable"
    try:
        client.head_object(Bucket=R2_BUCKET, Key=key)
        return "exists"
    except Exception as exc:
        code = (getattr(exc, "response", {}) or {}).get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey"):
            return "missing"
        logger.error("object_status check failed for key=%r: %s", key, exc)
        return "unavailable"


def object_exists(key: str) -> bool:
    """Check whether an object exists at the given key.

    Returns False when R2 is disabled or the object is not found (404).
    For any other error (403, network timeout, credential failure) it logs
    an error and returns False — callers treat a missing object as a cache
    miss, so we degrade gracefully instead of propagating transient errors.
    """
    return object_status(key) == "exists"


def object_etag(key: str) -> str | None:
    """Return the normalized object ETag, or None when unavailable."""
    client = _get_client()
    if client is None or not key:
        return None
    try:
        value = client.head_object(Bucket=R2_BUCKET, Key=key).get("ETag")
        return str(value or "").strip().strip('"') or None
    except Exception as exc:
        logger.warning("[R2] ETag unavailable key=%r: %s", key, exc)
        return None


def upload_file(local_path: str, key: str) -> Optional[str]:
    """Upload a local file to an arbitrary R2 key (used for cache, etc).
    Returns the key on success, None if R2 disabled. Raises on real errors."""
    client = _get_client()
    if client is None:
        return None
    content_type = _guess_content_type(key) or "application/octet-stream"
    client.upload_file(
        local_path, R2_BUCKET, key,
        ExtraArgs={"ContentType": content_type},
        Config=_transfer_config(),
    )
    return key


def download_object(key: str, dest_path: str) -> bool:
    """Download an R2 object to a local path. Returns True on success, False
    if R2 is disabled or the download fails (caller decides what to do)."""
    client = _get_client()
    if client is None:
        return False
    os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
    try:
        client.download_file(R2_BUCKET, key, dest_path)
        size_mb = os.path.getsize(dest_path) / 1024 / 1024
        logger.info("[R2] Downloaded %s -> %s (%.1f MB)", key, dest_path, size_mb)
        return True
    except Exception as e:
        logger.error("[R2] Download failed for %s: %s", key, e)
        return False


def generate_signed_url(
    key: str,
    expiry_seconds: int = 3600,
    *,
    download_filename: str | None = None,
    response_content_type: str | None = None,
) -> Optional[str]:
    """Pre-signed GET URL for the stored object. None if R2 is disabled.

    Pass `download_filename` to force R2 to send Content-Disposition:
    attachment so the browser downloads the file instead of opening it.
    Always set this for ProRes/MOV masters downloaded by the user.
    """
    client = _get_client()
    if client is None:
        return None
    params: dict = {"Bucket": R2_BUCKET, "Key": key}
    if download_filename:
        params["ResponseContentDisposition"] = (
            f'attachment; filename="{download_filename}"'
        )
    if response_content_type:
        # Multipart browser uploads historically landed in R2 as
        # application/octet-stream. That is fine for downloads, but Chromium
        # can abort progressive <audio> playback after its initial buffer when
        # a WAV is served as a generic binary. Override the response metadata
        # in the signed URL without mutating the immutable source object.
        params["ResponseContentType"] = response_content_type
    return client.generate_presigned_url(
        "get_object",
        Params=params,
        ExpiresIn=expiry_seconds,
    )


def presign_put_url(
    tenant_id: str,
    job_id: str,
    filename: str,
    *,
    content_type: Optional[str] = None,
    expiry_seconds: int = 900,
) -> Optional[dict]:
    """Pre-signed PUT URL for a single-shot upload directly from the
    browser to R2. Returns {"url": str, "key": str, "expires_in": int}
    or None when R2 is not configured.

    The browser sends `PUT <url>` with the file body and a matching
    `Content-Type` header (the URL is signed against that content_type
    so altering it invalidates the signature). The API container never
    sees the body — that's the whole point of this path.
    """
    client = _get_client()
    if client is None:
        return None
    key = _input_object_key(tenant_id, job_id, filename)
    params = {"Bucket": R2_BUCKET, "Key": key}
    if content_type:
        params["ContentType"] = content_type
    url = client.generate_presigned_url(
        "put_object", Params=params, ExpiresIn=expiry_seconds,
    )
    return {"url": url, "key": key, "expires_in": expiry_seconds}


def presign_put_object_key(
    key: str,
    *,
    content_type: Optional[str] = None,
    expiry_seconds: int = 900,
) -> Optional[dict]:
    """Sign an already-scoped object key.

    Campaign audio is registered before a Job exists, so it cannot use the
    historical ``inputs/<tenant>/<job>`` key builder. Callers must construct
    and tenant-scope the key; this helper only signs it.
    """
    client = _get_client()
    if client is None:
        return None
    params = {"Bucket": R2_BUCKET, "Key": key}
    if content_type:
        params["ContentType"] = content_type
    url = client.generate_presigned_url(
        "put_object", Params=params, ExpiresIn=expiry_seconds,
    )
    return {"url": url, "key": key, "expires_in": expiry_seconds}


def multipart_init(
    tenant_id: str,
    job_id: str,
    filename: str,
    *,
    content_type: Optional[str] = None,
) -> Optional[dict]:
    """Begin a multipart upload. Returns {"upload_id", "key"} or None
    when R2 is disabled.

    Multipart is the right tool for >16 MB uploads on flaky connections:
    each part is a separate PUT, parts can be uploaded in parallel, and
    a failed part retries without re-sending the whole file. Keep an
    upload_id around until the operator confirms completion (via
    `multipart_complete`) — abandoned multipart uploads waste R2 storage
    and need to be aborted by the reaper.
    """
    client = _get_client()
    if client is None:
        return None
    key = _input_object_key(tenant_id, job_id, filename)
    args = {"Bucket": R2_BUCKET, "Key": key}
    if content_type:
        args["ContentType"] = content_type
    try:
        resp = client.create_multipart_upload(**args)
    except Exception as exc:
        # boto3 ClientError or network error to R2. Without this guard the
        # exception bubbles up as an unhandled 500 with no body, which the
        # browser then misreports as a CORS error. Returning None lets the
        # endpoint emit a proper 503 with a useful message.
        import logging
        logging.getLogger(__name__).error(
            "multipart_init failed for key=%s bucket=%s: %s",
            key, R2_BUCKET, exc, exc_info=True,
        )
        return None
    return {"upload_id": resp["UploadId"], "key": key}


def multipart_init_object_key(
    key: str,
    *,
    content_type: Optional[str] = None,
) -> Optional[dict]:
    """Begin multipart upload for a pre-scoped campaign object key."""
    client = _get_client()
    if client is None:
        return None
    args = {"Bucket": R2_BUCKET, "Key": key}
    if content_type:
        args["ContentType"] = content_type
    try:
        response = client.create_multipart_upload(**args)
    except Exception as exc:
        logger.error(
            "campaign multipart_init failed key=%s: %s", key, exc,
            exc_info=True,
        )
        return None
    return {"upload_id": response["UploadId"], "key": key}


def multipart_presign_part(
    key: str, upload_id: str, part_number: int,
    *, expiry_seconds: int = 900,
) -> Optional[str]:
    """Pre-signed URL for a single multipart part. Browser PUTs the
    part bytes against this URL and reads the `ETag` header from the
    response — that ETag goes back in `multipart_complete`."""
    client = _get_client()
    if client is None:
        return None
    return client.generate_presigned_url(
        "upload_part",
        Params={
            "Bucket": R2_BUCKET,
            "Key": key,
            "UploadId": upload_id,
            "PartNumber": part_number,
        },
        ExpiresIn=expiry_seconds,
    )


_TRANSIENT_BOTOCORE_EXC_NAMES = (
    "EndpointConnectionError",
    "ReadTimeoutError",
    "ConnectionClosedError",
    "ConnectTimeoutError",
    "IncompleteReadError",
)


def _is_transient_boto_error(exc: BaseException) -> bool:
    """True if the exception is a network/timeout error worth retrying.

    Complements boto3's built-in retry (configured in _get_client with
    max_attempts=5, adaptive mode). boto3 already handles most 5xx and
    throttling, but a few connection-level errors slip through —
    especially on Railway when egress to R2 has a brief hiccup. We add
    a short retry on top so the operator doesn't have to wait the full
    frontend backoff (1s → 2s → 4s …) before recovery.
    """
    if exc.__class__.__name__ in _TRANSIENT_BOTOCORE_EXC_NAMES:
        return True
    # botocore.exceptions.ClientError with a 5xx response also counts.
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        if isinstance(status, int) and status >= 500:
            return True
    return False


def _retry_transient(fn, label: str, max_attempts: int = 3, base_delay: float = 0.5):
    """Run `fn()`, retrying on transient boto errors with exponential backoff.

    Total max wait at default config: 0.5 + 1.0 = 1.5 s before final
    attempt. Cheap compared to the frontend's per-part retry (~1-32 s
    backoff). Permanent errors (4xx other than throttling) propagate on
    the first hit so we don't burn time on hopeless retries.
    """
    import logging
    import time as _time
    last_exc: BaseException | None = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — bounded by transient check below
            if not _is_transient_boto_error(exc):
                raise
            last_exc = exc
            if attempt == max_attempts - 1:
                break
            wait = base_delay * (2 ** attempt)
            logging.getLogger(__name__).warning(
                "[R2] %s transient error (attempt %d/%d), retrying in %.1fs: %s",
                label, attempt + 1, max_attempts, wait, exc,
            )
            _time.sleep(wait)
    assert last_exc is not None
    raise last_exc


def put_object_bytes(key: str, data, content_type: str = "application/octet-stream") -> bool:
    """Upload to R2 from raw bytes OR a seekable file-like object.

    `data` historically was bytes; the proxy handlers now pass a
    SpooledTemporaryFile so the worker can yield to the event loop
    while the body streams in from a slow upstream. boto3 accepts
    both natively. On retry we rewind the file (seek(0)) since
    boto3 doesn't auto-reset on its own.
    """
    client = _get_client()
    if client is None:
        return False

    def _do_put():
        if hasattr(data, "seek"):
            data.seek(0)
        return client.put_object(
            Bucket=R2_BUCKET,
            Key=key,
            Body=data,
            ContentType=content_type,
        )

    try:
        _retry_transient(_do_put, label=f"put_object key={key}")
        return True
    except Exception as exc:
        import logging
        logging.getLogger(__name__).error(
            "put_object_bytes failed for key=%s: %s", key, exc, exc_info=True
        )
        return False


def upload_part(
    key: str, upload_id: str, part_number: int, data,
) -> Optional[str]:
    """Upload one multipart part from bytes OR a seekable file-like.
    Returns ETag (without quotes) or None. Same dual-input contract as
    put_object_bytes — see there for rationale."""
    client = _get_client()
    if client is None:
        return None

    def _do_upload():
        if hasattr(data, "seek"):
            data.seek(0)
        return client.upload_part(
            Bucket=R2_BUCKET,
            Key=key,
            UploadId=upload_id,
            PartNumber=part_number,
            Body=data,
        )

    try:
        response = _retry_transient(
            _do_upload,
            label=f"upload_part key={key} part={part_number}",
        )
        return response["ETag"].strip('"')
    except Exception as exc:
        import logging
        logging.getLogger(__name__).error("upload_part failed: %s", exc, exc_info=True)
        return None


def multipart_complete(
    key: str, upload_id: str, parts: list[dict],
) -> Optional[str]:
    """Finalize a multipart upload. `parts` is a list of
    {"PartNumber": int, "ETag": str} dicts (one per uploaded part,
    sorted by PartNumber). Returns the key on success."""
    client = _get_client()
    if client is None:
        return None
    sorted_parts = sorted(parts, key=lambda p: int(p["PartNumber"]))
    try:
        client.complete_multipart_upload(
            Bucket=R2_BUCKET,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={"Parts": sorted_parts},
        )
    except Exception as exc:
        response = getattr(exc, "response", None)
        error = response.get("Error", {}) if isinstance(response, dict) else {}
        code = str(error.get("Code") or "")
        is_no_such_upload = (
            code == "NoSuchUpload" or "NoSuchUpload" in str(exc)
        )
        message = (
            "multipart_complete failed key=%r upload_id=%r: %s "
            "(caller reconciles via head_object_size)"
        )
        if is_no_such_upload:
            # Expected after a duplicate completion: the first call consumes
            # the upload id. The caller HEADs the object and distinguishes
            # durable idempotent success from an expired upload.
            logger.warning(message, key, upload_id, exc, exc_info=True)
        else:
            # Auth, outage, 5xx and unknown storage failures remain Sentry
            # events even though the caller still attempts reconciliation.
            logger.error(message, key, upload_id, exc, exc_info=True)
        return None
    return key


def multipart_last_activity(key: str, upload_id: str):
    """Return the most recent ``LastModified`` across the parts already
    uploaded for an in-flight multipart upload, or ``None`` when the
    upload has no parts yet, doesn't exist anymore, or listing fails.

    Used by the reaper to distinguish "abandoned upload" (no parts
    landing for a while) from "slow but alive upload" (a 150 MB WAV on a
    residential uplink can legitimately take longer than the awaiting-
    upload TTL). The browser batch-presigns every part at init and PUTs
    straight to R2, so R2 itself is the only place this activity signal
    exists — nothing touches our API between init and complete.
    """
    newest = None
    marker = 0
    try:
        client = _get_client()
        if client is None:
            return None
        while True:
            resp = client.list_parts(
                Bucket=R2_BUCKET, Key=key, UploadId=upload_id,
                MaxParts=1000, PartNumberMarker=marker,
            )
            for part in resp.get("Parts", []):
                lm = part.get("LastModified")
                if lm is not None and (newest is None or lm > newest):
                    newest = lm
            if not resp.get("IsTruncated"):
                break
            marker = resp.get("NextPartNumberMarker", 0)
            if not marker:
                break
    except Exception as e:
        # NoSuchUpload (already aborted/completed) or transient listing
        # failure — either way there's no liveness evidence to report.
        logger.debug(
            "[R2] list_parts %s %s failed: %s", key, upload_id, e,
        )
        return None
    return newest


def multipart_list_parts(key: str, upload_id: str) -> Optional[list[dict]]:
    """Return completed parts, or ``None`` when the upload no longer exists.

    An empty list is a valid newly-created upload. ``None`` lets the campaign
    API replace an expired/aborted upload id instead of handing the local
    uploader presigned URLs that can never succeed.
    """
    client = _get_client()
    if client is None:
        return None
    marker = 0
    completed: list[dict] = []
    try:
        while True:
            response = client.list_parts(
                Bucket=R2_BUCKET, Key=key, UploadId=upload_id,
                MaxParts=1000, PartNumberMarker=marker,
            )
            completed.extend({
                "part_number": int(part["PartNumber"]),
                "etag": str(part["ETag"]).strip('"'),
                "size": int(part.get("Size") or 0),
            } for part in response.get("Parts", []))
            if not response.get("IsTruncated"):
                break
            marker = int(response.get("NextPartNumberMarker") or 0)
            if not marker:
                break
    except Exception as exc:
        logger.warning("[R2] could not list resumable parts key=%s: %s", key, exc)
        return None
    return completed


def head_object_size(key: str) -> Optional[int]:
    """Return the stored object's ContentLength in bytes, or ``None`` if
    R2 is disabled, the object doesn't exist, or the HEAD fails.

    The upload-size gate in /upload-url trusts the CLIENT-declared
    size_bytes; the presigned PUT itself doesn't constrain the body, so
    this is the server-side source of truth for what actually landed.
    """
    try:
        client = _get_client()
        if client is None:
            return None
        resp = client.head_object(Bucket=R2_BUCKET, Key=key)
        return int(resp.get("ContentLength", 0))
    except Exception as e:
        logger.debug("[R2] head_object %s failed: %s", key, e)
        return None


def multipart_abort(key: str, upload_id: str) -> bool:
    """Abort an in-flight multipart upload. Best-effort — returns False
    if R2 is disabled or the abort fails (the orphan still costs R2
    storage; the periodic abort sweep cleans it up)."""
    client = _get_client()
    if client is None:
        return False
    try:
        client.abort_multipart_upload(
            Bucket=R2_BUCKET, Key=key, UploadId=upload_id,
        )
        return True
    except Exception as e:
        logger.error("[R2] multipart_abort %s %s failed: %s", key, upload_id, e)
        return False


def abort_stale_multipart_uploads(
    older_than_hours: int = 24, prefix: str = "inputs/",
) -> dict:
    """Abort multipart uploads initiated more than `older_than_hours`
    ago. This IS the "periodic abort sweep" that the multipart_abort
    docstring referenced — hasta 2026-07-02 no existía, así que los
    huérfanos (complete fallido, abort ignorado, supersede mid-upload)
    acumulaban storage R2 para siempre.

    24 h de umbral es deliberadamente conservador: la subida legítima
    más larga (150 MB en uplink residencial + reintentos) se mide en
    decenas de minutos, y el guard del reaper (upload_still_active) ya
    protege a las vivas — cualquier upload_id de ayer es basura segura.

    Returns {"scanned": n, "aborted": n, "failed": n}.
    """
    from datetime import datetime, timedelta, timezone
    report = {"scanned": 0, "aborted": 0, "failed": 0}
    try:
        client = _get_client()
        if client is None:
            return report
        cutoff = datetime.now(timezone.utc) - timedelta(hours=older_than_hours)
        paginator = client.get_paginator("list_multipart_uploads")
        for page in paginator.paginate(Bucket=R2_BUCKET, Prefix=prefix):
            for up in page.get("Uploads", []):
                report["scanned"] += 1
                initiated = up.get("Initiated")
                if initiated is None or initiated >= cutoff:
                    continue
                try:
                    client.abort_multipart_upload(
                        Bucket=R2_BUCKET,
                        Key=up["Key"],
                        UploadId=up["UploadId"],
                    )
                    report["aborted"] += 1
                    logger.info(
                        "[R2] stale multipart aborted: %s (initiated %s)",
                        up["Key"], initiated,
                    )
                except Exception as e:
                    report["failed"] += 1
                    logger.warning(
                        "[R2] stale multipart abort failed %s: %s",
                        up["Key"], e,
                    )
    except Exception as e:
        logger.warning("[R2] stale multipart sweep failed: %s", e)
    return report


def _active_input_keys() -> "set[str] | None":
    """Return the input-key prefixes of EVERY job that still has a DB row.

    Cleanup must NOT touch these. Only an ORPHAN input — one whose job row no
    longer exists (the job was deleted) — is safe to purge; any job with a row
    may still need its input for a timing edit, re-render, or retry. (Changed
    2026-06-03 from a fragile per-status allow-list, after repeated
    "audio no disponible" reports caused by purging live jobs' inputs.)

    Returns:
      - a set (possibly EMPTY when there are genuinely zero jobs) on success;
      - None when the DB couldn't be read (import/query failure). The caller
        MUST treat None as "protection unknown → ABORT", NOT as "nothing to
        protect" — otherwise a transient DB blip mid-sweep would treat every
        input as an orphan and purge live jobs (the agus.cafisi incident). An
        empty set and None are deliberately distinct.
    """
    try:
        from database import Job, SessionLocal
    except Exception:
        return None  # DB layer unavailable → caller must ABORT, not purge-all

    keys: set[str] = set()
    try:
        db = SessionLocal()
        try:
            # ORPHAN-ONLY (2026-06-03): protect the input of EVERY job that
            # still has a DB row, regardless of status. The old per-status
            # allow-list (queued/done/error/…) was fragile — any status NOT on
            # the list let a live job's input be purged, breaking timing edits
            # and re-renders (recurring "audio no disponible" reports from
            # agus.cafisi). The ONLY input safe to delete is an ORPHAN — one
            # whose job row no longer exists (the job was deleted). So protect
            # them all; cleanup then removes only truly-orphaned objects.
            # Select just the two columns we need (not full rows).
            for tenant_id, job_id in db.query(Job.tenant_id, Job.job_id).all():
                # The worker writes inputs under inputs/{tenant}/{job_id}/.
                # Match the prefix (not the exact key) to handle filename
                # rewrites at upload time.
                keys.add(f"inputs/{_safe_filename(tenant_id)}/{_safe_filename(job_id)}/")
        finally:
            db.close()
    except Exception:
        # DB hiccup mid-query — we CANNOT trust a partial/empty protect set
        # (treating unread jobs as orphans would purge live inputs). Signal
        # "unknown" so the caller aborts the sweep.
        return None
    return keys


def cleanup_old_inputs(retention_days: int = 365, apply: bool = False, prefix: str = "inputs/") -> dict:
    """Delete objects under `prefix` whose LastModified is older than
    retention_days. Returns a structured report:

        {
            "scanned": int,                    # total keys under prefix
            "expired": int,                    # keys older than cutoff
            "deleted": int,                    # actually removed (apply=True)
            "bytes_freed": int,                # sum of sizes that were/would-be deleted
            "sample": [{"key", "size", "age_days"}, ...],  # up to 10 candidates
            "errors": [...],
            "apply": bool,
            "retention_days": int,
            "cutoff": str,
        }

    Set apply=False (default) to dry-run. Caller is responsible for not
    widening `prefix` past inputs/ — deliverables live elsewhere and must
    not be touched by retention.
    """
    from datetime import datetime, timedelta, timezone

    client = _get_client()
    if client is None:
        return {"error": "R2 not configured", "scanned": 0, "expired": 0,
                "deleted": 0, "bytes_freed": 0, "sample": [], "errors": [],
                "apply": apply, "retention_days": retention_days}

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    paginator = client.get_paginator("list_objects_v2")

    # Build a protect-list of input keys for EVERY live job (orphan-only
    # retention). We skip these even if past the retention window.
    protected_prefixes = _active_input_keys()
    if protected_prefixes is None:
        # FAIL-SAFE: the DB was unreadable, so we cannot tell which inputs
        # belong to live jobs. Deleting now would treat every input as an
        # orphan and purge live jobs (the agus.cafisi incident). ABORT — an
        # empty protect-set (no jobs) is fine, but None (unknown) is not.
        logger.error("[R2] cleanup_old_inputs ABORTED — could not read protected "
                     "job inputs from DB (refusing to purge to avoid live-job loss)")
        return {"error": "could not determine protected inputs (DB unreadable) — "
                         "aborted to avoid purging live jobs",
                "scanned": 0, "expired": 0, "deleted": 0, "bytes_freed": 0,
                "sample": [], "errors": [], "apply": apply,
                "retention_days": retention_days}
    skipped_active = 0

    scanned = 0
    expired: list[tuple[str, int, "datetime"]] = []
    bytes_to_free = 0

    for page in paginator.paginate(Bucket=R2_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []) or []:
            scanned += 1
            modified = obj["LastModified"]
            if modified < cutoff:
                key = obj["Key"]
                if any(key.startswith(p) for p in protected_prefixes):
                    skipped_active += 1
                    continue
                expired.append((key, obj["Size"], modified))
                bytes_to_free += obj["Size"]

    now = datetime.now(timezone.utc)
    sample = [
        {
            "key": k,
            "size_mb": round(s / 1024 / 1024, 2),
            "age_days": (now - m).days,
        }
        for (k, s, m) in expired[:10]
    ]

    deleted = 0
    errors: list[dict] = []
    if apply and expired:
        # AUDIT TRAIL 2026-05-27: emit a loud WARNING listing every key
        # we're about to delete. Critical for forensics after the
        # agus.cafisi incident (26 inputs vanished, no audit trail to
        # tell us which sweep did it). One log line per key keeps grep
        # friendly even when the batch is 1000+.
        import sys as _sys
        try:
            caller_frame = _sys._getframe(1)
            caller = f"{caller_frame.f_code.co_filename.split('/')[-1]}:{caller_frame.f_lineno}"
        except Exception:
            caller = "<unknown>"
        logger.warning(
            "[R2-BULK-DELETE] cleanup_old_inputs about to delete %d keys "
            "(retention_days=%d, prefix=%r, caller=%s)",
            len(expired), retention_days, prefix, caller,
        )
        for k, sz, mod in expired:
            logger.warning(
                "[R2-BULK-DELETE-KEY] key=%r size_mb=%.1f age_days=%d retention_days=%d",
                k, sz / 1024 / 1024, (now - mod).days, retention_days,
            )
        if len(expired) >= R2_CLEANUP_SPIKE_THRESHOLD:
            # An unusually large orphan sweep is actionable, but ordinary
            # retention work must not create one Sentry issue per run.
            try:
                import sentry_sdk
                with sentry_sdk.push_scope() as _scope:
                    _scope.fingerprint = ["r2-bulk-delete-spike", caller, prefix or ""]
                    _scope.set_tag("event", "r2.cleanup_spike")
                    _scope.set_tag("r2.caller", caller)
                    _scope.set_tag("r2.prefix", prefix or "")
                    _scope.set_extra("r2.expired_count", len(expired))
                    _scope.set_extra("r2.spike_threshold", R2_CLEANUP_SPIKE_THRESHOLD)
                    _scope.set_extra("r2.retention_days", retention_days)
                    sentry_sdk.capture_message(
                        f"[R2-BULK-DELETE] cleanup spike via {caller} (prefix={prefix})",
                        level="warning",
                    )
            except Exception:
                pass
        for i in range(0, len(expired), 1000):
            batch = expired[i:i + 1000]
            resp = client.delete_objects(
                Bucket=R2_BUCKET,
                Delete={
                    "Objects": [{"Key": k} for (k, _, _) in batch],
                    "Quiet": False,
                },
            )
            deleted += len(resp.get("Deleted", []) or [])
            errors.extend(resp.get("Errors", []) or [])

    return {
        "apply": apply,
        "retention_days": retention_days,
        "prefix": prefix,
        "cutoff": cutoff.isoformat(timespec="seconds"),
        "scanned": scanned,
        "expired": len(expired),
        "deleted": deleted,
        "skipped_active": skipped_active,
        "bytes_freed": bytes_to_free if apply else 0,
        "bytes_to_free_dryrun": bytes_to_free if not apply else 0,
        "sample": sample,
        "errors": errors,
    }


def delete_prefix(prefix: str, *, max_objects: int = 500) -> dict:
    """Delete every object under `prefix`. Used to fully clean up a job's
    deliverable folder (`{tenant}/{job_id}/`) on hard-delete, including
    edit-version snapshots (`.v1`, `.v2`, ...) that live outside job.s3_keys
    and were never covered by the plain delete_object(key) calls in
    jobs._delete_r2_objects — see the 2026-08 audit that found ~260GB of
    orphaned deliverables from deleted test jobs sitting in R2 forever
    because only the current 5 canonical keys got cleaned up.

    Deliberately NOT usable on 'inputs/' — that tree is shared across
    variants/edits (see jobs._delete_r2_objects docstring) and has its own
    reference-counted deletion path. Callers must pass a job-scoped
    deliverables prefix, not a tenant- or bucket-wide one.

    max_objects is a hard safety cap: a prefix that somehow resolves to more
    than a single job's worth of files aborts instead of bulk-deleting, so a
    caller bug (e.g. an empty/wrong prefix) can't wipe a whole tenant.
    """
    empty = {"prefix": prefix, "deleted": 0, "errors": [], "bytes_freed": 0, "aborted": False}
    if not prefix or prefix in ("/", "") or prefix.startswith("inputs/"):
        logger.error("delete_prefix refused unsafe prefix=%r", prefix)
        empty["aborted"] = True
        return empty
    client = _get_client()
    if client is None:
        return empty

    keys: list[tuple[str, int]] = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=R2_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []) or []:
            keys.append((obj["Key"], obj.get("Size", 0)))
            if len(keys) > max_objects:
                logger.error(
                    "delete_prefix aborted: prefix=%r has more than %d objects "
                    "(safety cap) — refusing to bulk-delete, investigate manually",
                    prefix, max_objects,
                )
                empty["aborted"] = True
                return empty
    if not keys:
        return empty

    import sys as _sys
    try:
        caller_frame = _sys._getframe(1)
        caller = f"{caller_frame.f_code.co_filename.split('/')[-1]}:{caller_frame.f_lineno}"
    except Exception:
        caller = "<unknown>"
    logger.warning(
        "[R2-DELETE-PREFIX] prefix=%r keys=%d bytes=%d caller=%s",
        prefix, len(keys), sum(sz for _, sz in keys), caller,
    )

    deleted = 0
    errors: list[dict] = []
    for i in range(0, len(keys), 1000):
        batch = keys[i:i + 1000]
        resp = client.delete_objects(
            Bucket=R2_BUCKET,
            Delete={"Objects": [{"Key": k} for k, _ in batch], "Quiet": False},
        )
        deleted += len(resp.get("Deleted", []) or [])
        errors.extend(resp.get("Errors", []) or [])

    return {
        "prefix": prefix,
        "deleted": deleted,
        "errors": errors,
        "bytes_freed": sum(sz for _, sz in keys),
        "aborted": False,
    }


def delete_object(key: str) -> None:
    client = _get_client()
    if client is None:
        return
    # AUDIT TRAIL 2026-05-27: log EVERY delete with WARNING level + caller
    # frame so any future "where did my audio go?" investigation can
    # grep the logs. Triggered after the agus.cafisi incident where 26
    # input audios were deleted from R2 between days 9-16 by some path
    # we couldn't trace from existing logs. The caller frame helps
    # identify which code path called us (jobs, reaper, cleanup, etc).
    if key.startswith("inputs/"):
        import sys as _sys
        try:
            caller_frame = _sys._getframe(1)
            caller = f"{caller_frame.f_code.co_filename.split('/')[-1]}:{caller_frame.f_lineno}"
        except Exception:
            caller = "<unknown>"
        logger.warning(
            "[R2-DELETE] input key=%r called_from=%s", key, caller,
            extra={"event": "r2_input_deleted", "key": key, "caller": caller},
        )
    try:
        client.delete_object(Bucket=R2_BUCKET, Key=key)
    except Exception as exc:
        logger.error("delete_object failed for key=%r: %s", key, exc, exc_info=True)
        # A failed delete is actionable: keep it visible in Sentry while
        # avoiding an issue for every normal reaper cleanup.
        try:
            import sentry_sdk
            with sentry_sdk.push_scope() as _scope:
                _scope.set_tag("event", "r2.delete_failed")
                _scope.set_extra("r2.key", key)
                sentry_sdk.capture_exception(exc)
        except Exception:
            # Observability must never turn a storage failure into a second
            # failure or hide the original exception from the caller.
            pass
        raise


def copy_object(src_key: str, dst_key: str) -> bool:
    """Server-side copy from src_key to dst_key within the same bucket.

    Used by run_edit_pipeline to archive the previous version of a deliverable
    (video/short/thumbnail) before the re-rendered file overwrites it. R2's
    copy_object completes without re-uploading bytes through us, so even
    multi-GB ProRes masters version in milliseconds.

    Returns True on success, False if R2 is disabled or the source key does
    not exist (treated as "nothing to archive"). Raises on real S3 errors so
    the caller can surface them.
    """
    from botocore.exceptions import (
        ClientError, ReadTimeoutError, ConnectTimeoutError,
    )
    client = _get_client()
    if client is None:
        return False
    if not object_exists(src_key):
        return False
    src = {"Bucket": R2_BUCKET, "Key": src_key}
    try:
        client.copy_object(Bucket=R2_BUCKET, Key=dst_key, CopySource=src)
    except ClientError as e:
        code = (e.response or {}).get("Error", {}).get("Code", "")
        # Single-operation CopyObject caps at 5 GB on S3/R2 → a multi-GB
        # ProRes master raises EntityTooLarge (incident: UMG edit-snapshot).
        # The managed client.copy() falls back to multipart UploadPartCopy,
        # which has no such limit.
        if code in ("EntityTooLarge", "InvalidRequest", "InvalidArgument"):
            logger.info("[R2] %s exceeds single-copy limit (%s) — using multipart copy", src_key, code)
            client.copy(CopySource=src, Bucket=R2_BUCKET, Key=dst_key)
        else:
            raise
    except (ReadTimeoutError, ConnectTimeoutError) as e:
        # Single-op server-side CopyObject holds ONE HTTP connection open for
        # the entire server-side copy; for a multi-GB ProRes master R2's
        # internal copy can exceed the client read_timeout (120s) →
        # ReadTimeoutError (incident 2026-06-09: "[EDIT] snapshot copy failed
        # for umg_master"). The docstring's "versions in milliseconds" holds
        # for small files, not GB-scale masters. Managed client.copy() chunks
        # into many small UploadPartCopy calls — each a fast server-side copy
        # well under the timeout — so the same multipart path that handles
        # EntityTooLarge also dodges the per-request timeout.
        logger.info("[R2] %s single-copy timed out (%s) — using multipart copy", src_key, type(e).__name__)
        client.copy(CopySource=src, Bucket=R2_BUCKET, Key=dst_key)
    logger.info("[R2] Copied %s -> %s", src_key, dst_key)
    return True


def _guess_content_type(filename: str) -> Optional[str]:
    low = filename.lower()
    if low.endswith(".mov"):
        return "video/quicktime"
    if low.endswith(".mp4"):
        return "video/mp4"
    if low.endswith(".m4a"):
        return "audio/mp4"
    if low.endswith(".jpg") or low.endswith(".jpeg"):
        return "image/jpeg"
    if low.endswith(".png"):
        return "image/png"
    return None
