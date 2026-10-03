"""Immutable R2 manifests for the exact cut exposed by a UMG portal.

Render outputs retain their legacy mutable keys for the editor. A portal
publication copies the approved cut once to a content-addressed key; Argentina
and Chile reuse that snapshot when they publish the same render. A signed URL
can therefore never start returning bytes from a later edit.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone

import delivery_freshness
import storage
from sqlalchemy import text


FILENAMES = {
    "umg_master": "umg_master.mov",
    "umg_short": "umg_short.mov",
    "video": "lyric_video.mp4",
    "short": "short.mp4",
    "thumbnail": "thumbnail.jpg",
}


class ManifestUnavailable(RuntimeError):
    """The approved source cut could not be frozen reliably."""


@dataclass(frozen=True)
class PublishedManifest:
    fingerprint: str
    manifest_hash: str
    keys: dict[str, str]
    etags: dict[str, str]
    sizes: dict[str, int]
    source_keys: dict[str, str]
    source_etags: dict[str, str]


def source_key(job, file_type: str) -> str:
    if file_type not in FILENAMES:
        raise ValueError(f"Unsupported delivery file type: {file_type}")
    tracked = (getattr(job, "s3_keys", None) or {}).get(file_type)
    if tracked:
        return tracked
    return (
        f"{storage._safe_filename(job.tenant_id)}/"
        f"{storage._safe_filename(job.job_id)}/"
        f"{storage._safe_filename(FILENAMES[file_type])}"
    )


def freeze_manifest(job, file_types: list[str]) -> PublishedManifest:
    """Copy and verify one render without mutating the published pointer.

    No DB transaction should be open during these potentially long R2 copies.
    The caller rechecks the job and source ETags under a short lock afterwards.
    A failed copy leaves the prior portal manifest intact.
    """
    types = list(dict.fromkeys(file_types))
    if not types or any(ft not in FILENAMES for ft in types):
        raise ManifestUnavailable("La entrega no tiene archivos válidos.")
    fingerprint = delivery_freshness.render_fingerprint(job)
    sources: dict[str, tuple[str, str, int]] = {}
    for ft in types:
        key = source_key(job, ft)
        etag = storage.object_etag(key)
        size = storage.head_object_size(key)
        if not etag or size is None:
            raise ManifestUnavailable(f"No se pudo verificar el archivo {ft}.")
        sources[ft] = (key, etag, size)
    identity = {ft: {"etag": row[1], "size": row[2]} for ft, row in sorted(sources.items())}
    manifest_hash = hashlib.sha256(json.dumps(
        {"job": job.job_id, "fingerprint": fingerprint, "files": identity},
        sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    prefix = (
        f"published/{storage._safe_filename(job.tenant_id)}/"
        f"{storage._safe_filename(job.job_id)}/{manifest_hash}"
    )
    keys: dict[str, str] = {}
    etags: dict[str, str] = {}
    sizes: dict[str, int] = {}
    for ft in types:
        src, original_etag, original_size = sources[ft]
        dst = f"{prefix}/{storage._safe_filename(FILENAMES[ft])}"
        if (storage.head_object_size(dst) != original_size
                or storage.object_source_etag(dst) != original_etag):
            if not storage.copy_object(src, dst, source_etag=original_etag):
                raise ManifestUnavailable(f"No se pudo preparar el archivo {ft}.")
        if storage.object_etag(src) != original_etag:
            raise ManifestUnavailable(
                f"El archivo {ft} cambió mientras se preparaba la publicación."
            )
        copied_size = storage.head_object_size(dst)
        copied_etag = storage.object_etag(dst)
        if (copied_size != original_size or not copied_etag
                or storage.object_source_etag(dst) != original_etag):
            raise ManifestUnavailable(f"No se pudo comprobar la copia de {ft}.")
        keys[ft] = dst
        etags[ft] = copied_etag
        sizes[ft] = copied_size
    for ft, (src, original_etag, _original_size) in sources.items():
        if storage.object_etag(src) != original_etag:
            raise ManifestUnavailable(
                f"El archivo {ft} cambió durante la preparación. Volvé a revisar el corte."
            )
    return PublishedManifest(
        fingerprint, manifest_hash, keys, etags, sizes,
        {ft: row[0] for ft, row in sources.items()},
        {ft: row[1] for ft, row in sources.items()},
    )


def assert_manifest_source_current(job, manifest: PublishedManifest) -> None:
    """Fence a prepared snapshot against a concurrent edit before commit."""
    if delivery_freshness.render_fingerprint(job) != manifest.fingerprint:
        raise ManifestUnavailable("El video cambió durante la preparación. Revisá el corte actual.")
    for ft, source in manifest.source_keys.items():
        if source_key(job, ft) != source or storage.object_etag(source) != manifest.source_etags[ft]:
            raise ManifestUnavailable(
                f"El archivo {ft} cambió durante la preparación. Revisá el corte actual."
            )


def published_key(delivery, file_type: str) -> str | None:
    """Resolve a portal file, preserving legacy rows until republished."""
    saved = getattr(delivery, "published_file_keys", None) or {}
    if saved.get(file_type):
        return saved[file_type]
    if getattr(delivery, "published_manifest_hash", None):
        # A damaged manifest must not silently fall back to a mutable render.
        return None
    return (
        f"{storage._safe_filename(delivery.tenant_snapshot)}/"
        f"{storage._safe_filename(delivery.job_id)}/"
        f"{storage._safe_filename(FILENAMES[file_type])}"
    )


def publish_record(
    ddb, *, job, portal_id: str, added_by: int, label: str,
    manifest: PublishedManifest, preserve_label: bool = False,
) -> tuple[object, bool, list[int], bool]:
    """Apply one portal publication in the deliveries DB transaction.

    Individual and campaign publication call this same function so version,
    UMG approval and request resolution have identical semantics. The advisory
    lock serializes first publishes, where a row-level lock cannot yet exist.
    """
    from database import Delivery, DeliveryChangeRequest

    if ddb.get_bind().dialect.name == "postgresql":
        ddb.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"delivery:{portal_id}:{job.job_id}"},
        )
    delivery = (
        ddb.query(Delivery)
        .filter(Delivery.job_id == job.job_id)
        .filter(Delivery.portal_id == portal_id)
        .filter(Delivery.removed_at.is_(None))
        .with_for_update()
        .first()
    )
    now = datetime.now(timezone.utc)
    created = delivery is None
    # A legacy row without a frozen manifest cannot prove that its approved
    # bytes equal this cut. Treat the first explicit publication as a new
    # revision; never carry an old UMG approval onto unverified bytes.
    changed = delivery is not None and (
        delivery_freshness.needs_publish(job, delivery)
        or delivery.published_manifest_hash != manifest.manifest_hash
    )
    if delivery is None:
        delivery = Delivery(
            job_id=job.job_id, portal_id=portal_id, published_revision=1,
            content_updated_at=now,
        )
        ddb.add(delivery)
    elif changed:
        prior = dict(delivery.published_file_keys or {})
        if prior and prior != manifest.keys:
            delivery.retired_file_keys = list(delivery.retired_file_keys or []) + [{
                "keys": prior, "retired_at": now.isoformat(),
            }]
        delivery.published_revision = (delivery.published_revision or 1) + 1
        delivery.content_updated_at = now
        delivery.approved_at = None
        delivery.approved_by_label = None
        delivery.approved_revision = None

    # Campaign retries keep an operator's label; the individual endpoint may
    # explicitly rename it and passes the already selected label here.
    delivery.label = delivery.label if preserve_label and not created and delivery.label else label
    delivery.file_types = list(manifest.keys)
    delivery.file_sizes = dict(manifest.sizes)
    delivery.artist_snapshot = job.artist
    delivery.song_title_snapshot = job.song_title or ""
    delivery.tenant_snapshot = job.tenant_id
    delivery.frame_size_snapshot = (job.umg_spec or {}).get("frame_size")
    delivery.added_by_user_id = added_by
    delivery.added_at = now
    delivery.published_render_fingerprint = manifest.fingerprint
    delivery.published_file_keys = dict(manifest.keys)
    delivery.published_file_etags = dict(manifest.etags)
    delivery.published_manifest_hash = manifest.manifest_hash
    delivery.stale_since = None
    delivery.stale_reason = None
    ddb.flush()

    resolved: list[int] = []
    pending = (
        ddb.query(DeliveryChangeRequest)
        .filter(DeliveryChangeRequest.delivery_id == delivery.id)
        .filter(DeliveryChangeRequest.resolved_at.is_(None))
        .with_for_update()
        .all()
    )
    from change_request_parser import SCHEMA_VERSION as parser_version
    for request in pending:
        # A retry of a cut already published may finish a verification that
        # raced the first publication. It may never close a request submitted
        # against this same published revision.
        newer_than_request = (
            request.requested_revision is not None
            and delivery.published_revision > request.requested_revision
        )
        if not changed and not newer_than_request:
            continue
        if (
            request.verified_render_fingerprint != manifest.fingerprint
            or request.verified_at is None
            or (request.verification_evidence or {}).get("parser_version") != parser_version
            or (request.verification_evidence or {}).get("request_sha256")
               != hashlib.sha256((request.comment or "").encode()).hexdigest()
            or request.verified_at.replace(tzinfo=request.verified_at.tzinfo or timezone.utc)
               < request.submitted_at.replace(tzinfo=request.submitted_at.tzinfo or timezone.utc)
        ):
            continue
        request.resolved_at = now
        request.resolved_by_user_id = added_by
        request.resolved_by_revision = delivery.published_revision
        request.resolution_source = "publication"
        request.resolution_note = f"Verificado y publicado en la versión {delivery.published_revision}."
        resolved.append(request.id)
    return delivery, changed, resolved, created
