"""Cloud object storage (Cloudflare R2, S3-compatible).

Moves rendered masters out of the local disk so (a) /download redirects to a
signed URL on R2 instead of streaming a 5 GB .mov through uvicorn, and (b) the
local outputs/ directory doesn't grow unbounded.

All helpers are no-ops when R2_* env vars are missing, so local dev still
works without cloud storage.
"""

import os
from typing import Optional

R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "").strip()
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "").strip()
R2_ENDPOINT_URL = os.environ.get("R2_ENDPOINT_URL", "").strip()
R2_BUCKET = os.environ.get("R2_BUCKET", "").strip()

_client = None


def is_enabled() -> bool:
    """True when all R2 env vars are present."""
    return bool(R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY and R2_ENDPOINT_URL and R2_BUCKET)


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
        config=Config(signature_version="s3v4", retries={"max_attempts": 3, "mode": "standard"}),
    )
    return _client


def _object_key(tenant_id: str, job_id: str, filename: str) -> str:
    return f"{tenant_id}/{job_id}/{filename}"


def _input_object_key(tenant_id: str, job_id: str, filename: str) -> str:
    """Inputs (user-uploaded MP3s) live under a separate prefix so lifecycle
    rules can purge them aggressively without touching deliverables."""
    return f"inputs/{tenant_id}/{job_id}/{filename}"


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
    client.upload_file(local_path, R2_BUCKET, key, ExtraArgs=extra)
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
        local_path, R2_BUCKET, key, ExtraArgs={"ContentType": content_type}
    )
    size_mb = os.path.getsize(local_path) / 1024 / 1024
    print(f"[R2] Uploaded input {key} ({size_mb:.1f} MB)")
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


def generate_signed_url(key: str, expiry_seconds: int = 3600) -> Optional[str]:
    """Pre-signed GET URL for the stored object. None if R2 is disabled."""
    client = _get_client()
    if client is None:
        return None
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": R2_BUCKET, "Key": key},
        ExpiresIn=expiry_seconds,
    )


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
