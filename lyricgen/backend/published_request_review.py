"""Bind a human decision to one request and the exact publication reviewed."""
import hashlib
import json


def review_context(job, delivery, request):
    from delivery_freshness import render_fingerprint
    from delivery_snapshots import portal_key

    if (job is None or delivery is None or delivery.removed_at is not None
            or request.resolved_at is not None
            or not delivery.published_render_fingerprint
            or not delivery.published_revision
            or 'video' not in (delivery.file_types or [])
            or not portal_key(delivery, 'video')):
        return None
    if delivery.published_file_keys is None and (
        job.status not in {'done', 'pending_review'}
        or render_fingerprint(job) != delivery.published_render_fingerprint
    ):
        # A legacy mutable key with no current render identity cannot be
        # presented as the particular published revision being reviewed.
        return None
    # This is a concurrency receipt, not authorization. In pointer/legacy
    # mode the working render can change the published bytes, so bind it too.
    identity = [
        request.id, request.comment, request.submitted_at, request.updated_at,
        delivery.id, delivery.job_id, delivery.portal_id,
        delivery.published_revision, delivery.published_render_fingerprint,
        delivery.published_file_keys, sorted(delivery.file_types or []),
        delivery.content_updated_at, render_fingerprint(job), job.status,
    ]
    token = hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode()).hexdigest()
    return {'review_token': token, 'published_revision': delivery.published_revision}
