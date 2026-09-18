"""Fail-closed publication snapshots, with an atomic DB pointer switch."""
from uuid import uuid4

import storage

FILENAMES = {'video': 'lyric_video.mp4', 'short': 'short.mp4',
             'thumbnail': 'thumbnail.jpg', 'umg_master': 'umg_master.mov',
             'umg_short': 'umg_short.mov'}


def working_key(tenant, job_id, file_type):
    return storage._object_key(tenant, job_id, FILENAMES[file_type])


def portal_key(delivery, file_type):
    keys = getattr(delivery, 'published_file_keys', None)
    if keys is not None:
        # A partial snapshot must not leak through to a newer working cut.
        return keys.get(file_type)
    return working_key(delivery.tenant_snapshot, delivery.job_id, file_type)


def copy_snapshot(tenant, job_id, file_types, *, allow_missing=False):
    snapshot = uuid4().hex
    result = {}
    for file_type in file_types:
        if file_type not in FILENAMES:
            continue
        source = working_key(tenant, job_id, file_type)
        if allow_missing:
            status = storage.object_status(source)
            if status == 'missing':
                continue
            if status != 'exists':
                raise RuntimeError('No se pudo verificar la versión publicada.')
        target = f'{source}.published-{snapshot}'
        if not storage.copy_object(source, target):
            raise RuntimeError('No se pudo conservar la versión publicada. No se actualizaron los entregables.')
        result[file_type] = target
    if not result and not allow_missing:
        raise RuntimeError('La publicación no contiene archivos.')
    return result


def pin_legacy_deliveries(job_id):
    """Called BEFORE overwriting working files. Failure aborts the upload.

    Never replace a pinned pointer: Argentina and Chile may have approved
    different cuts. Partial copies are harmless unreferenced objects.
    """
    from database import Delivery, scoped_deliveries_db
    with scoped_deliveries_db() as db:
        rows = (db.query(Delivery).filter(Delivery.job_id == job_id,
                Delivery.removed_at.is_(None)).with_for_update().all())
        for row in rows:
            if row.published_file_keys is None:
                row.published_file_keys = copy_snapshot(
                    row.tenant_snapshot, job_id, row.file_types or [], allow_missing=True)
        db.commit()
