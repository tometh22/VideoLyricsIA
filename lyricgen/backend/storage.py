"""Cloud object storage (Cloudflare R2, S3-compatible).

Moves rendered masters out of the local disk so (a) /download redirects to a
signed URL on R2 instead of streaming a 5 GB .mov through uvicorn, and (b) the
local outputs/ directory doesn't grow unbounded.

All helpers are no-ops when R2_* env vars are missing, so local dev still
works without cloud storage. Operators following the docker-compose file
typically set S3_* env vars instead — those are accepted as fallbacks so
the same compose file works for both R2 and any S3-compatible backend.
"""

import os
import re
from typing import Optional


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
        ),
    )
    return _client


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


def _object_key(tenant_id: str, job_id: str, filename: str) -> str:
    return f"{_safe_filename(tenant_id)}/{_safe_filename(job_id)}/{_safe_filename(filename)}"


def _input_object_key(tenant_id: str, job_id: str, filename: str) -> str:
    """Inputs (user-uploaded MP3s) live under a separate prefix so lifecycle
    rules can purge them aggressively without touching deliverables."""
    return (
        f"inputs/{_safe_filename(tenant_id)}"
        f"/{_safe_filename(job_id)}/{_safe_filename(filename)}"
    )


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
    print(f"[R2] Uploaded {key} ({size_mb:.1f} MB)")
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
    print(f"[R2] Uploaded input {key} ({size_mb:.1f} MB)")
    return key


def object_exists(key: str) -> bool:
    """Check whether an object exists at the given key. False on R2 disabled
    or any error (treated as cache miss)."""
    client = _get_client()
    if client is None:
        return False
    try:
        client.head_object(Bucket=R2_BUCKET, Key=key)
        return True
    except Exception:
        return False


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
        print(f"[R2] Downloaded {key} -> {dest_path} ({size_mb:.1f} MB)")
        return True
    except Exception as e:
        print(f"[R2] Download failed for {key}: {e}")
        return False


def generate_signed_url(
    key: str,
    expiry_seconds: int = 3600,
    *,
    download_filename: str | None = None,
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
    resp = client.create_multipart_upload(**args)
    return {"upload_id": resp["UploadId"], "key": key}


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
    client.complete_multipart_upload(
        Bucket=R2_BUCKET,
        Key=key,
        UploadId=upload_id,
        MultipartUpload={"Parts": sorted_parts},
    )
    return key


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
        print(f"[R2] multipart_abort {key} {upload_id} failed: {e}")
        return False


def _active_input_keys() -> set[str]:
    """Return the set of object keys that belong to a non-terminal job.

    Cleanup must NOT touch these — a job that has been queued for >30 days
    after an outage still has its input MP3 referenced from the DB, and
    deleting it from R2 makes the job unrunnable when the worker finally
    picks it up.

    Empty set is returned when the DB is unreachable; the caller treats
    that as "no protected keys" and skips deletion entirely (see below).
    """
    try:
        from database import Job, SessionLocal
    except Exception:
        return set()

    keys: set[str] = set()
    try:
        db = SessionLocal()
        try:
            non_terminal = (
                db.query(Job)
                .filter(Job.status.in_((
                    "queued", "processing", "pending_review",
                    # Inputs of awaiting_upload / transcribed_pending jobs
                    # must not be GC'd — the user is still in the middle of
                    # the upload-edit-generate flow. The reaper handles
                    # short-TTL cleanup separately.
                    "awaiting_upload", "transcribed_pending",
                )))
                .all()
            )
            for j in non_terminal:
                # Whatever the original upload filename was, the worker
                # writes inputs under inputs/{tenant}/{job_id}/. Match the
                # prefix rather than the exact key to handle filename
                # rewrites at upload time.
                keys.add(f"inputs/{_safe_filename(j.tenant_id)}/{_safe_filename(j.job_id)}/")
        finally:
            db.close()
    except Exception:
        # DB hiccup — return what we have; caller may decide to abort.
        pass
    return keys


def cleanup_old_inputs(retention_days: int = 30, apply: bool = False, prefix: str = "inputs/") -> dict:
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

    # Build a protect-list of input keys belonging to jobs that are still
    # queued/processing/pending_review. We skip these even if their
    # LastModified is past the retention window.
    protected_prefixes = _active_input_keys()
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


def delete_object(key: str) -> None:
    client = _get_client()
    if client is None:
        return
    client.delete_object(Bucket=R2_BUCKET, Key=key)


def _guess_content_type(filename: str) -> Optional[str]:
    low = filename.lower()
    if low.endswith(".mov"):
        return "video/quicktime"
    if low.endswith(".mp4"):
        return "video/mp4"
    if low.endswith(".jpg") or low.endswith(".jpeg"):
        return "image/jpeg"
    if low.endswith(".png"):
        return "image/png"
    return None
