"""A correction variant updates the requested delivery, not an unrelated option."""
from fastapi import HTTPException
from sqlalchemy import or_
from database import Delivery, DeliveryChangeRequest, Job
from change_request_workflow import latest_overwrite, timestamp


def identity(row):
    if row is None:
        return None
    return (row.id, row.job_id, row.published_revision,
            row.published_render_fingerprint, row.published_file_keys,
            row.file_types, row.added_at, row.content_updated_at, row.removed_at)


def target(db, ddb, job, portal):
    """Unique same-song ancestor with an open request wins over a duplicate.

    No title-only matching: unrelated options, tenants and portals are untouched.
    Call again after media I/O and compare identities before committing.
    """
    scope = or_(Delivery.portal_id == portal, Delivery.portal_id.is_(None)) if portal == 'argentina' else Delivery.portal_id == portal
    active = ddb.query(Delivery).filter(scope, Delivery.removed_at.is_(None))
    own = active.filter(Delivery.job_id == job.job_id).populate_existing().first()
    ancestors = []
    seen = {job.job_id}
    parent_id = job.parent_job_id
    while parent_id and parent_id not in seen and len(seen) < 20:
        seen.add(parent_id)
        parent = db.query(Job).filter(Job.job_id == parent_id).first()
        if (parent is None or parent.tenant_id != job.tenant_id
                or (parent.artist or '').strip().casefold() != (job.artist or '').strip().casefold()
                or (parent.song_title or '').strip().casefold() != (job.song_title or '').strip().casefold()):
            break
        ancestors.append(parent_id)
        parent_id = parent.parent_job_id
    rendered_at = latest_overwrite(job) or timestamp(job.completed_at)
    candidates = active.filter(Delivery.job_id.in_(ancestors)).filter(
        Delivery.id.in_(ddb.query(DeliveryChangeRequest.delivery_id).filter(
            DeliveryChangeRequest.resolved_at.is_(None),
            DeliveryChangeRequest.submitted_at <= rendered_at)),
    ).populate_existing().all() if ancestors and rendered_at else []
    if not candidates:
        return own, None
    if len(candidates) != 1 or (own and ddb.query(DeliveryChangeRequest.id).filter(
            DeliveryChangeRequest.delivery_id == own.id,
            DeliveryChangeRequest.resolved_at.is_(None)).first()):
        raise HTTPException(status_code=409, detail='Hay varios pedidos vinculados a esta variante. Elegí la entrega a corregir antes de publicar.')
    return candidates[0], own


def archive_duplicate(duplicate, now):
    """Retain files, video and audit history; hide only the duplicate portal row."""
    if duplicate is not None:
        duplicate.removed_at = now
