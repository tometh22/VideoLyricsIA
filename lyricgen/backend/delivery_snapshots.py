"""Publication of deliverables: frozen snapshots, or a pointer to the latest render.

Two modes, chosen by ``PUBLISH_LATEST_POINTER``:

* off (default): every publication COPIES the files into immutable
  ``.published-<uuid>`` objects (minutes and several GB per portal) and the
  portal serves that frozen copy. Fail-closed, but slow and duplicated.
* on: publishing copies nothing. ``published_file_keys`` stays NULL and the
  portal serves the job's current render directly, so the newest cut is always
  what the client downloads and a publication is a few database writes.
"""
import os
from uuid import uuid4

import storage

PRORES_TYPES = frozenset({'umg_master', 'umg_short'})

FILENAMES = {'video': 'lyric_video.mp4', 'short': 'short.mp4',
             'thumbnail': 'thumbnail.jpg', 'umg_master': 'umg_master.mov',
             'umg_short': 'umg_short.mov'}


def working_key(tenant, job_id, file_type):
    return storage._object_key(tenant, job_id, FILENAMES[file_type])


def latest_pointer_enabled() -> bool:
    """Publish without copying; serve the job's newest render (see module doc)."""
    return os.environ.get('PUBLISH_LATEST_POINTER', '').strip().lower() in {'1', 'true', 'yes', 'on'}


def portal_key(delivery, file_type):
    keys = getattr(delivery, 'published_file_keys', None)
    if keys is not None:
        # A partial snapshot must not leak through to a newer working cut.
        return keys.get(file_type)
    if latest_pointer_enabled() and file_type in PRORES_TYPES:
        # A re-render leaves the PREVIOUS broadcast master in R2 until the new
        # one is transcoded. While the delivery is marked in flight, serving
        # that key would hand the client the old cut next to a new MP4.
        from delivery_freshness import STALE_IN_FLIGHT
        if getattr(delivery, 'stale_since', None) and getattr(delivery, 'stale_reason', None) in STALE_IN_FLIGHT:
            return None
    return working_key(delivery.tenant_snapshot, delivery.job_id, file_type)


def copy_snapshot(tenant, job_id, file_types, *, allow_missing=False):
    """Copy a stable observed source set; never return a partial verified set.

    This detects source changes during publication. It cannot establish that
    sources already present before preflight came from the same render: that
    requires a writer-owned immutable generation manifest. Multipart ETags are
    used as preconditions only, never compared to destination content hashes.
    """
    snapshot = uuid4().hex
    result = {}
    sources = {}
    absent_sources = []
    # Capture ALL identities before any copy, so a late missing/unknown source
    # does not start an expensive partially usable publication.
    for file_type in dict.fromkeys(file_types):
        if file_type not in FILENAMES:
            continue
        source = working_key(tenant, job_id, file_type)
        identity = storage.object_identity(source)
        if identity.get('status') == 'missing' and allow_missing:
            absent_sources.append(source)
            continue
        if identity.get('status') != 'exists':
            raise RuntimeError('No se pudo verificar la versión publicada.')
        sources[file_type] = (source, identity)
    for file_type, (source, identity) in sources.items():
        target = f'{source}.published-{snapshot}'
        if not storage.copy_object(source, target, expected_etag=identity['etag']):
            raise RuntimeError('No se pudo conservar la versión publicada. No se actualizaron los entregables.')
        destination = storage.object_identity(target)
        if destination.get('status') != 'exists' or destination.get('size') != identity['size']:
            raise RuntimeError('No se pudo verificar la copia publicada. No se actualizaron los entregables.')
        result[file_type] = target
    # A source may change after its own copy while another large master copies.
    # Recheck the full set after all copies, before returning publishable keys.
    for source, identity in sources.values():
        if storage.object_identity(source) != identity:
            raise RuntimeError('Los archivos cambiaron mientras se preparaba la publicación. Volvé a revisar el corte.')
    if any(storage.object_identity(source).get('status') != 'missing' for source in absent_sources):
        raise RuntimeError('Los archivos cambiaron mientras se preparaba la publicación. Volvé a revisar el corte.')
    if not result and not allow_missing:
        raise RuntimeError('La publicación no contiene archivos.')
    return result


def pin_legacy_deliveries(job_id):
    """Called BEFORE overwriting working files. Failure aborts the upload.

    Never replace a pinned pointer: Argentina and Chile may have approved
    different cuts. Partial copies are harmless unreferenced objects.

    Pointer mode never freezes anything: the portal is meant to follow the
    newest render, so there is nothing to conserve (and no multi-GB copy to
    run before every re-render).
    """
    if latest_pointer_enabled():
        return
    from database import Delivery, scoped_deliveries_db
    with scoped_deliveries_db() as db:
        rows = (db.query(Delivery).filter(Delivery.job_id == job_id,
                Delivery.removed_at.is_(None)).all())
        def identity(row):
            return (row.tenant_snapshot, tuple(row.file_types or []),
                    row.published_revision, row.published_render_fingerprint,
                    row.added_at, row.content_updated_at)
        pending = [(row.id, identity(row)) for row in rows
                   if row.published_file_keys is None]
        db.rollback()
        for row_id, expected in pending:
            # Keep no transaction/lock alive during large R2 copies. Another
            # publisher may win; never overwrite its pinned pointer.
            keys = copy_snapshot(expected[0], job_id, list(expected[1]), allow_missing=True)
            row = (db.query(Delivery).filter(Delivery.id == row_id)
                   .populate_existing().with_for_update().first())
            if row is None or row.removed_at is not None or row.published_file_keys is not None:
                db.rollback()
                continue
            if identity(row) != expected:
                db.rollback()
                raise RuntimeError('La entrega cambió mientras se conservaba la versión publicada.')
            row.published_file_keys = keys
            db.commit()
