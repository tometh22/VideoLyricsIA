"""On-demand ProRes exports of the current portal publication.

Resolve the source through the publication policy, including live pointers,
and fence exports by its ETag so corrections cannot reuse an old master.
"""
import hashlib
import json
import math
import os
import tempfile

import storage
from delivery_snapshots import portal_key

SOURCES = {"umg_master": "video", "umg_short": "short"}
ACTIVE_STATES = {"queued", "started", "deferred", "scheduled"}
MAX_QUEUE_DEPTH = int(os.environ.get("PORTAL_PRORES_MAX_QUEUE_DEPTH", "20"))


class QueueBusy(RuntimeError):
    pass


def file_types(delivery):
    types = list(delivery.file_types or [])
    keys = getattr(delivery, "published_file_keys", None)
    for derivative, source in SOURCES.items():
        if source in types and (keys is None or keys.get(source)) and derivative not in types:
            types.append(derivative)
    return types


def published_master(delivery, file_type):
    # Unpinned legacy working masters cannot prove which cut they encode.
    # Resolve derivatives by the current MP4 identity without freezing pointers.
    if delivery.published_file_keys is None:
        return None
    return portal_key(delivery, file_type, for_client=True)


def ready_key(delivery, file_type):
    """Pinned masters are reusable; live pointers require the current MP4 ETag."""
    master = published_master(delivery, file_type)
    if master and storage.object_status(master) == "exists":
        return master
    source = portal_key(delivery, SOURCES[file_type], for_client=True)
    etag = storage.object_etag(source) if source else None
    if not etag:
        return None
    _, target = export_identity(source, etag, file_type)
    return target if storage.object_status(target) == "exists" else None


def export_identity(source_key, etag, file_type):
    digest = hashlib.sha256(json.dumps(
        [source_key, etag, file_type, "native-422-hq-v1"],
        separators=(",", ":"),
    ).encode()).hexdigest()
    parent = source_key.rsplit("/", 1)[0]
    return f"portal-prores-{digest}", f"{parent}/.portal-prores/{digest}/{file_type}.mov"


def _task_state(connection, task_id):
    from rq.job import Job
    from rq.exceptions import NoSuchJobError
    try:
        task = Job.fetch(task_id, connection=connection)
        status = task.get_status(refresh=True)
        return getattr(status, "value", status)
    except NoSuchJobError:
        return "not_found"


def task_state(task_id):
    from queue_jobs import _init_redis
    connection, _, _ = _init_redis()
    if connection is None:
        raise RuntimeError("ProRes queue unavailable")
    return _task_state(connection, task_id)


def enqueue(delivery_id, portal_id, file_type, source_key, etag):
    from queue_jobs import (
        _init_redis, _require_submissions_open, _evict_stale_rq_job, rq_payload_metadata,
        PRORES_PREWARM_TIMEOUT, RESULT_TTL, FAILURE_TTL,
    )
    from rq import Retry
    _require_submissions_open()
    connection, _, queue = _init_redis()
    if connection is None or queue is None:
        raise RuntimeError("ProRes queue unavailable")
    task_id, target = export_identity(source_key, etag, file_type)
    # RQ's deterministic ids alone do not deduplicate concurrent enqueue calls.
    # Serialize the check+enqueue across API replicas. Failed attempts can retry.
    with connection.lock(task_id + "-enqueue", timeout=15, blocking_timeout=3):
        state = _task_state(connection, task_id)
        if state in ACTIVE_STATES:
            return task_id
        if queue.count >= MAX_QUEUE_DEPTH:
            raise QueueBusy("Hay varios archivos en preparación. Reintentá en unos minutos.")
        if state != "not_found":
            _evict_stale_rq_job(connection, task_id)
        queue.enqueue(
            "portal_prores.materialize",
            args=(delivery_id, portal_id, file_type, source_key, etag, target),
            job_id=task_id,
            job_timeout=PRORES_PREWARM_TIMEOUT,
            result_ttl=RESULT_TTL,
            failure_ttl=FAILURE_TTL,
            retry=Retry(max=2, interval=[30, 60]),
            meta=rq_payload_metadata("portal_prores"),
        )
    return task_id


def _source_spec(path):
    from pipeline import _probe_dims_fps
    from render_spec import RenderSpec
    dimensions = _probe_dims_fps(path)
    if not dimensions:
        raise RuntimeError("No se pudo verificar el video publicado.")
    width, height, rate = dimensions
    # Preserve the source resolution and frame rate, including fractional FPS
    # and vertical shorts. No guessed 24/30fps and no resizing a published cut.
    from fractions import Fraction
    fps = float(Fraction(rate))
    if width <= 0 or height <= 0 or not math.isfinite(fps) or fps <= 0:
        raise RuntimeError("El video publicado tiene un formato inválido.")
    divisor = math.gcd(width, height)
    class NativeSpec(RenderSpec):
        @property
        def fps_str(self):
            return rate

    return NativeSpec(
        profile="umg", width=width, height=height, fps=fps,
        dar=(width // divisor, height // divisor), codec="prores_ks",
        prores_profile=3, pix_fmt="yuv422p10le", audio_codec="pcm_s24le",
        color_primaries="bt709", container="mov",
    )


def materialize(delivery_id, portal_id, file_type, source_key, etag, target):
    """RQ worker: transcode the validated published source into a separate key."""
    from database import Delivery, DeliveriesSessionLocal
    from pipeline import _transcode_to_prores
    if file_type not in SOURCES or export_identity(source_key, etag, file_type)[1] != target:
        raise ValueError("Invalid portal export")
    source_type = SOURCES[file_type]

    def current(db, *, lock=False):
        query = db.query(Delivery).filter(
            Delivery.id == delivery_id, Delivery.removed_at.is_(None),
        )
        query = query.filter(
            (Delivery.portal_id == "argentina") | Delivery.portal_id.is_(None)
        ) if portal_id == "argentina" else query.filter(Delivery.portal_id == portal_id)
        row = (query.with_for_update() if lock else query).first()
        if not row or portal_key(row, source_type, for_client=True) != source_key:
            raise RuntimeError("La publicación cambió durante la preparación.")
        return row

    with DeliveriesSessionLocal() as db:
        current(db)
    if storage.object_etag(source_key) != etag:
        raise RuntimeError("El video publicado cambió durante la preparación.")

    target_status = storage.object_status(target)
    if target_status == "unavailable":
        raise RuntimeError("No se pudo verificar el almacenamiento del ProRes.")
    if target_status != "exists":
        # A unique temp directory prevents editor/prewarm/replica cache races,
        # and cleanup removes the multi-GB local MOV on success or failure.
        with tempfile.TemporaryDirectory(prefix="portal-prores-") as directory:
            source_path = os.path.join(directory, "source.mp4")
            output_path = os.path.join(directory, "master.mov")
            if not storage.download_object(source_key, source_path):
                raise RuntimeError("No se pudo leer el video publicado.")
            if storage.object_etag(source_key) != etag:
                raise RuntimeError("El video publicado cambió durante la descarga.")
            _transcode_to_prores(source_path, output_path, _source_spec(source_path))
            if storage.object_etag(source_key) != etag:
                raise RuntimeError("El video publicado cambió durante la preparación.")
            if not storage.upload_file(output_path, target):
                raise RuntimeError("No se pudo guardar el ProRes.")

    # Hold no DB session during download, ffmpeg or multi-GB upload. A correction
    # that publishes while we render must not acquire this old cut's MOV.
    with DeliveriesSessionLocal() as db:
        row = current(db, lock=True)
        if storage.object_etag(source_key) != etag:
            raise RuntimeError("El video publicado cambió durante la preparación.")
        # Pointer publications must continue following future corrected renders.
        # Never freeze them by attaching this export to published_file_keys.
        if row.published_file_keys is not None:
            row.published_file_keys = {**row.published_file_keys, file_type: target}
            row.file_types = list(dict.fromkeys([*(row.file_types or []), file_type]))
            db.commit()
    return target
