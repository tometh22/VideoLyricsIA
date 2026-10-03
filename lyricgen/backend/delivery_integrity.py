"""Read-only integrity check for the exact objects a UMG portal signs."""

from __future__ import annotations

from delivery_manifest import published_key
import storage


def inspect_delivery(delivery) -> dict:
    """Return ok/failed/unknown; a network error is never reported as healthy."""
    files = []
    manifest_hash = delivery.published_manifest_hash
    if not manifest_hash:
        return {
            "delivery_id": delivery.id, "portal_id": delivery.portal_id,
            "job_id": delivery.job_id, "status": "unknown",
            "reason": "legacy_publication_without_immutable_manifest", "files": [],
        }
    for ft in delivery.file_types or []:
        key = published_key(delivery, ft)
        expected_prefix = (
            f"published/{storage._safe_filename(delivery.tenant_snapshot)}/"
            f"{storage._safe_filename(delivery.job_id)}/{manifest_hash}/"
        )
        if not key or not key.startswith(expected_prefix):
            files.append({"type": ft, "status": "failed", "reason": "manifest_key_missing"})
            continue
        object_state = storage.object_status(key)
        if object_state == "missing":
            files.append({"type": ft, "status": "failed", "reason": "object_missing"})
            continue
        if object_state != "exists":
            files.append({"type": ft, "status": "unknown", "reason": "storage_unavailable"})
            continue
        actual_size = storage.head_object_size(key)
        actual_etag = storage.object_etag(key)
        expected_size = (delivery.file_sizes or {}).get(ft)
        expected_etag = (delivery.published_file_etags or {}).get(ft)
        if actual_size is None or actual_etag is None or expected_size is None or not expected_etag:
            files.append({"type": ft, "status": "unknown", "reason": "identity_unavailable"})
        elif actual_size != expected_size or actual_etag != expected_etag:
            files.append({"type": ft, "status": "failed", "reason": "identity_mismatch"})
        else:
            files.append({"type": ft, "status": "ok"})
    status = ("failed" if any(row["status"] == "failed" for row in files)
              else "unknown" if not files or any(row["status"] == "unknown" for row in files)
              else "ok")
    return {
        "delivery_id": delivery.id, "portal_id": delivery.portal_id,
        "job_id": delivery.job_id, "revision": delivery.published_revision,
        "manifest_hash": manifest_hash, "status": status, "files": files,
        "update_pending": delivery.stale_since is not None,
    }
