"""Generate a portal ProRes on demand and pin it to the published cut."""

import logging
import os

import delivery_freshness
import storage
from database import Delivery, DeliveriesSessionLocal, Job, SessionLocal
from delivery_snapshots import copy_snapshot
from prores import OUTPUTS_DIR, _SOURCE_MP4, ensure_prores_exists

logger = logging.getLogger(__name__)
PRORES_TYPES = frozenset(("umg_master", "umg_short"))


def materialize_delivery_prores(
    delivery_id: int, portal_id: str, file_type: str, fingerprint: str,
) -> str:
    """RQ entrypoint. Never attach a master to a different published render."""
    if file_type not in PRORES_TYPES:
        raise ValueError("Invalid ProRes type")

    ddb = DeliveriesSessionLocal()
    db = SessionLocal()
    try:
        delivery = (ddb.query(Delivery).filter(
            Delivery.id == delivery_id, Delivery.portal_id == portal_id,
            Delivery.removed_at.is_(None),
        ).first())
        if not delivery or delivery.label != "Art Track" or file_type not in (delivery.file_types or []):
            raise ValueError("Art Track delivery is no longer available")
        keys = dict(delivery.published_file_keys or {})
        if keys.get(file_type) and storage.object_status(keys[file_type]) == "exists":
            return keys[file_type]
        job = db.query(Job).filter(
            Job.job_id == delivery.job_id,
            Job.tenant_id == delivery.tenant_snapshot,
        ).first()
        if (not job or not job.umg_spec or job.status != "done"
                or not fingerprint or delivery.published_render_fingerprint != fingerprint
                or delivery_freshness.render_fingerprint(job) != fingerprint):
            raise ValueError("The published render changed; publish the new version first")
        source_type = "video" if file_type == "umg_master" else "short"
        source_key = keys.get(source_type)
        if not source_key or storage.object_status(source_key) != "exists":
            raise ValueError("The published MP4 source is unavailable")
        job_id, tenant_id, job_data = job.job_id, job.tenant_id, job.to_dict()
    finally:
        db.close()
        ddb.close()

    # Existing job key is a useful cache only while the approved render still
    # matches. Otherwise download the immutable published MP4 before checking
    # a local .mov: an old worker cache with no local source looks "fresh" to
    # the generic ProRes helper and could serve the wrong cut.
    working_key = (job_data.get("s3_keys") or {}).get(file_type)
    local_path = None
    if not working_key or storage.object_status(working_key) != "exists":
        source_filename, _ = _SOURCE_MP4[file_type]
        source_path = os.path.join(OUTPUTS_DIR, job_id, source_filename)
        os.makedirs(os.path.dirname(source_path), exist_ok=True)
        if not storage.download_object(source_key, source_path):
            raise RuntimeError("Could not load the published MP4")
        os.utime(source_path, None)
        # This may take minutes. Hold no DB connection during ffmpeg/upload.
        local_path = ensure_prores_exists(job_id, file_type, job_data, tenant_id)

    db = SessionLocal()
    ddb = DeliveriesSessionLocal()
    try:
        job = db.query(Job).filter(Job.job_id == job_id, Job.tenant_id == tenant_id).first()
        delivery = (ddb.query(Delivery).filter(
            Delivery.id == delivery_id, Delivery.portal_id == portal_id,
            Delivery.removed_at.is_(None),
        ).with_for_update().first())
        if (not job or not delivery or delivery.label != "Art Track"
                or delivery.published_render_fingerprint != fingerprint
                or delivery_freshness.render_fingerprint(job) != fingerprint):
            raise ValueError("The render changed during ProRes preparation")
        keys = dict(delivery.published_file_keys or {})
        if keys.get(file_type) and storage.object_status(keys[file_type]) == "exists":
            return keys[file_type]
        working_key = (job.s3_keys or {}).get(file_type)
        if (not working_key or storage.object_status(working_key) != "exists") and local_path:
            from delivery_snapshots import FILENAMES
            working_key = storage.upload_master(
                local_path, tenant_id, job_id, FILENAMES[file_type],
            )
        if not working_key or storage.object_status(working_key) != "exists":
            raise RuntimeError("The ProRes could not be stored")
        pinned = copy_snapshot(tenant_id, job_id, [file_type])
        db.refresh(job)
        if job.status != "done" or delivery_freshness.render_fingerprint(job) != fingerprint:
            raise ValueError("The render changed while the ProRes was being published")
        keys.update(pinned)
        delivery.published_file_keys = keys
        size = storage.head_object_size(keys[file_type])
        if size is not None:
            delivery.file_sizes = {**(delivery.file_sizes or {}), file_type: size}
        ddb.commit()
        logger.info("[PORTAL] pinned %s for delivery %s", file_type, delivery_id)
        return keys[file_type]
    finally:
        db.close()
        ddb.close()
