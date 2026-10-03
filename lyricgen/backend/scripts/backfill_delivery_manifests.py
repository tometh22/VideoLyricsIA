"""Freeze legacy UMG publications after the manifest schema is deployed.

Dry run by default. Run against the job DB that owns the UMG jobs while
DELIVERIES_DATABASE_URL points to the shared portal DB. Rows whose published
cut cannot be proven identical to the current job are reported and skipped.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import delivery_freshness
from database import Delivery, DeliveriesSessionLocal, Job, SessionLocal
from delivery_manifest import (
    ManifestUnavailable, assert_manifest_source_current, freeze_manifest, source_key,
)
import storage


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Copy files and save manifests")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    jobs_db = SessionLocal()
    portal_db = DeliveriesSessionLocal()
    counts = {"eligible": 0, "frozen": 0, "skipped": 0, "failed": 0}
    try:
        query = portal_db.query(Delivery.id).filter(
            Delivery.removed_at.is_(None), Delivery.published_manifest_hash.is_(None),
        ).order_by(Delivery.id)
        if args.limit > 0:
            query = query.limit(args.limit)
        delivery_ids = [row.id for row in query.all()]
        portal_db.rollback()
        for delivery_id in delivery_ids:
            delivery = portal_db.query(Delivery).filter(Delivery.id == delivery_id).first()
            if delivery is None or delivery.removed_at or delivery.published_manifest_hash:
                counts["skipped"] += 1
                portal_db.rollback()
                continue
            job = jobs_db.query(Job).filter(Job.job_id == delivery.job_id).first()
            if not job or job.status != "done" or delivery.stale_since is not None:
                counts["skipped"] += 1
                portal_db.rollback()
                jobs_db.rollback()
                continue
            fingerprint = delivery_freshness.render_fingerprint(job)
            # Historical timestamp/no-edit heuristics do not prove which bytes
            # UMG saw. Both publication fingerprint and approved video ETag
            # must match the current cut, otherwise reconcile manually.
            if (delivery.published_render_fingerprint != fingerprint
                    or not job.approved_video_etag
                    or storage.object_etag(source_key(job, "video")) != job.approved_video_etag):
                counts["skipped"] += 1
                portal_db.rollback()
                jobs_db.rollback()
                continue
            file_types = list(delivery.file_types or [])
            counts["eligible"] += 1
            if not args.apply:
                portal_db.rollback()
                jobs_db.rollback()
                continue
            try:
                jobs_db.expunge(job)
                portal_db.rollback()
                jobs_db.rollback()
                manifest = freeze_manifest(job, file_types)
                current_job = jobs_db.query(Job).filter(Job.job_id == job.job_id).with_for_update().one()
                current_delivery = portal_db.query(Delivery).filter(Delivery.id == delivery_id).with_for_update().one()
                if (current_delivery.removed_at or current_delivery.published_manifest_hash
                        or current_delivery.stale_since is not None
                        or current_delivery.published_render_fingerprint != fingerprint
                        or list(current_delivery.file_types or []) != file_types
                        or current_job.status != "done"
                        or current_job.approved_video_etag != manifest.source_etags.get("video")):
                    raise ManifestUnavailable("La entrega cambió durante el backfill")
                assert_manifest_source_current(current_job, manifest)
                current_delivery.published_file_keys = manifest.keys
                current_delivery.published_file_etags = manifest.etags
                current_delivery.published_manifest_hash = manifest.manifest_hash
                current_delivery.file_sizes = manifest.sizes
                portal_db.commit()
                jobs_db.rollback()
                counts["frozen"] += 1
            except Exception as exc:
                portal_db.rollback()
                jobs_db.rollback()
                counts["failed"] += 1
                print(f"delivery {delivery.id}: {type(exc).__name__}", file=sys.stderr)
        print(counts)
        return 0 if counts["failed"] == 0 else 1
    finally:
        portal_db.close()
        jobs_db.close()


if __name__ == "__main__":
    raise SystemExit(main())
