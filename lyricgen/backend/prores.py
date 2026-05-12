"""ProRes export helpers.

Shared by the lazy /download path (uvicorn) and the optional pre-warm
worker (RQ) so both share the same idempotency + concurrency
guarantees. The transcode itself lives in pipeline._transcode_to_prores;
this module owns the lock + atomic rename that serialise parallel
callers.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    pass

logger = logging.getLogger("genly.prores")

# Output paths mirror main.py — kept here so the worker can import
# without pulling the whole FastAPI module.
OUTPUTS_DIR = os.path.join(os.path.dirname(__file__), "..", "outputs")

FILE_MAP_PRORES = {
    "umg_master": "umg_master.mov",
    "umg_short": "umg_short.mov",
}

# Source MP4 for each ProRes variant.
_SOURCE_MP4 = {
    "umg_master": ("lyric_video.mp4", "video"),
    "umg_short": ("short.mp4", "short"),
}


# In-process locks for the lazy ProRes transcode. Two parallel callers
# on the same (job_id, file_type) must NOT both spawn ffmpeg — they'd
# compete on the same output path and the post-transcode validator
# would catch a corrupt half-written file. Combined with the .tmp +
# os.replace pattern below, this is also safe across multiple uvicorn
# worker processes: only one process can rename to the final path at
# a time, and the loser sees os.path.exists(file_path) on its retry
# and skips its own transcode.
_PRORES_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_PRORES_LOCKS_GUARD = threading.Lock()


def _prores_lock_for(job_id: str, file_type: str) -> threading.Lock:
    key = (job_id, file_type)
    with _PRORES_LOCKS_GUARD:
        lock = _PRORES_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PRORES_LOCKS[key] = lock
    return lock


class ProResMisconfigured(Exception):
    """The job did not request UMG delivery, so ProRes is not available."""


class ProResSourceMissing(Exception):
    """The source MP4 needed to transcode is gone (not local, not in R2)."""


def ensure_prores_exists(
    job_id: str,
    file_type: str,
    job: dict,
    tenant_id: str,
) -> str:
    """Materialise the .mov for `file_type` (umg_master | umg_short).

    Returns the local file path on success. Idempotent and concurrency-
    safe: parallel callers serialise on a per-(job_id, file_type) lock,
    and even across processes the .tmp + os.replace handshake guarantees
    only one ffmpeg invocation reaches the final path. Used by both the
    lazy /download path and the optional pre-warm worker so the two
    cannot trip over each other.

    Raises ProResMisconfigured if the job has no umg_spec, or
    ProResSourceMissing if the source MP4 is unavailable. Any other
    exception (ffmpeg failure, validator rejection) is re-raised by the
    underlying _transcode_to_prores.
    """
    if file_type not in FILE_MAP_PRORES:
        raise ValueError(
            f"ensure_prores_exists: unsupported file_type {file_type!r}"
        )

    # Local imports keep this module light enough to import from the
    # worker without dragging the FastAPI app or moviepy globals.
    import storage
    from pipeline import _transcode_to_prores, _short_prores_spec
    from render_spec import RenderSpec
    from jobs import update_job

    file_path = os.path.join(OUTPUTS_DIR, job_id, FILE_MAP_PRORES[file_type])
    if os.path.exists(file_path):
        return file_path

    umg_spec = job.get("umg_spec")
    if not umg_spec:
        raise ProResMisconfigured(
            "This job did not request UMG delivery; ProRes not available."
        )

    source_filename, source_key_name = _SOURCE_MP4[file_type]
    source_path = os.path.join(OUTPUTS_DIR, job_id, source_filename)

    lock = _prores_lock_for(job_id, file_type)
    with lock:
        # Double-check inside the lock: a sibling caller may have
        # finished the transcode while we were waiting.
        if os.path.exists(file_path):
            return file_path

        if not os.path.exists(source_path):
            source_key = (job.get("s3_keys") or {}).get(source_key_name)
            if source_key and storage.is_enabled():
                os.makedirs(os.path.dirname(source_path), exist_ok=True)
                if not storage.download_object(source_key, source_path):
                    raise ProResSourceMissing(
                        f"Source {source_filename} not available locally or in R2."
                    )
            else:
                raise ProResSourceMissing(
                    f"Source {source_filename} not found; cannot generate ProRes."
                )

        spec = (
            RenderSpec.umg(**umg_spec) if file_type == "umg_master"
            else _short_prores_spec(umg_spec)
        )
        # ffmpeg writes to .tmp; we rename atomically once the post-
        # transcode validator is happy. Two processes may race on the
        # same source but only one's os.replace lands. The loser's .tmp
        # is overwritten or unlinked below.
        tmp_path = f"{file_path}.tmp"
        try:
            _transcode_to_prores(source_path, tmp_path, spec)
            os.replace(tmp_path, file_path)
        finally:
            # If transcode raised mid-way, drop the partial.
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        # Best-effort R2 upload so future downloads of this ProRes skip
        # the transcode entirely. Don't fail the caller if it errors.
        try:
            if storage.is_enabled():
                key = storage.upload_master(
                    file_path, tenant_id, job_id, FILE_MAP_PRORES[file_type],
                )
                if key:
                    # Re-read s3_keys FRESH from the DB before merging.
                    # The `job` dict was snapshot in prewarm_prores BEFORE
                    # the 60-300 s transcode; if the OTHER ProRes prewarm
                    # (umg_master vs umg_short — both are enqueued together
                    # in main.py:enable_prores_for_job) finished while ours
                    # was running, it already wrote its key to s3_keys.
                    # Using the stale snapshot here would overwrite that
                    # key — the file stays in R2 but the row reports it as
                    # missing, and the app shows "Generando ProRes..."
                    # forever. Confirmed in prod 2026-05-12: 8 of 18 done-
                    # UMG jobs had a phantom-missing key (reconciled in a
                    # one-off SQL script).
                    from jobs import get_job_model
                    from database import SessionLocal
                    _db = SessionLocal()
                    try:
                        _fresh = get_job_model(_db, job_id)
                        current_keys = dict(
                            (_fresh.s3_keys if _fresh else None) or {}
                        )
                    finally:
                        _db.close()
                    current_keys[file_type] = key
                    update_job(job_id, s3_keys=current_keys)
        except Exception as e:  # pragma: no cover
            logger.warning("[PRORES] R2 upload skipped: %s", e)

    return file_path


def prewarm_prores(job_id: str, file_type: str) -> str | None:
    """Worker entrypoint for the optional pre-warm flow (G4).

    Loads the Job from Postgres, calls ensure_prores_exists, and
    returns the local path on success or None if the job is no longer
    in a state that needs the .mov (e.g. deleted, never-UMG, missing
    source). Designed to be enqueued with `enqueue_prores_prewarm`
    just before run_pipeline marks the job done — when the customer
    eventually clicks "Master ProRes", the .mov is already on R2.

    Idempotent: if the lazy /download path already produced the file
    while this worker was queued, ensure_prores_exists short-circuits
    on os.path.exists. Returns the path either way.
    """
    from jobs import get_job_model
    from database import SessionLocal

    db = SessionLocal()
    try:
        model = get_job_model(db, job_id)
        if model is None:
            logger.info("[PRORES] prewarm: job %s vanished; skipping", job_id)
            return None
        # Snapshot the data we need before closing the session — calling
        # ensure_prores_exists outside the session avoids holding a
        # connection for the full ffmpeg run.
        job = model.to_dict()
        tenant_id = model.tenant_id
    finally:
        db.close()

    try:
        path = ensure_prores_exists(job_id, file_type, job, tenant_id)
        logger.info("[PRORES] prewarm: %s/%s ready at %s", job_id, file_type, path)
        return path
    except (ProResMisconfigured, ProResSourceMissing) as e:
        # These are normal "this job is not eligible for ProRes
        # prewarm" outcomes — log and exit cleanly without raising,
        # so the RQ job ends in `finished` not `failed`.
        logger.info("[PRORES] prewarm skipped for %s/%s: %s",
                    job_id, file_type, e)
        return None


# ---------------------------------------------------------------------------
# Non-blocking download status — used by /download/{id}/umg_master so a
# uvicorn worker is never tied up for the 60-300 s of a 4K@60 transcode.
# ---------------------------------------------------------------------------

class ProResReadiness:
    """Result of `check_prores_readiness`. Tells the API whether to serve
    the file, redirect to R2, wait briefly, or return 202 + Retry-After."""

    READY_LOCAL = "ready_local"
    READY_R2 = "ready_r2"
    IN_PROGRESS = "in_progress"
    NOT_STARTED = "not_started"
    MISCONFIGURED = "misconfigured"
    SOURCE_MISSING = "source_missing"

    def __init__(self, state: str, *, local_path: str | None = None,
                 retry_after_seconds: int | None = None,
                 detail: str | None = None):
        self.state = state
        self.local_path = local_path
        self.retry_after_seconds = retry_after_seconds
        self.detail = detail


def _short_wait_for_lock(job_id: str, file_type: str, max_wait_seconds: float = 15.0) -> bool:
    """If another caller is mid-transcode, wait briefly for it to finish.

    Returns True iff the lock is acquired within max_wait. Reusing the
    lock here would deadlock with the prewarm worker that's holding it,
    so we just check `lock.locked()` and poll for `os.path.exists` on
    the final path. 15 s catches the common end-of-transcode case
    without tying up the request thread.
    """
    import time
    final_path = os.path.join(OUTPUTS_DIR, job_id, FILE_MAP_PRORES[file_type])
    lock = _prores_lock_for(job_id, file_type)
    if not lock.locked():
        return False
    deadline = time.time() + max_wait_seconds
    while time.time() < deadline:
        if os.path.exists(final_path):
            return True
        time.sleep(0.5)
    return False


def check_prores_readiness(
    job_id: str,
    file_type: str,
    job: dict,
    tenant_id: str,
    *,
    short_wait_seconds: float = 15.0,
) -> ProResReadiness:
    """Inspect whether the .mov is ready to serve, in progress, or needs
    a fresh enqueue. Designed for an HTTP request thread — never runs
    ffmpeg, never blocks for more than `short_wait_seconds`. The API
    layer translates the result into 200/302/202/400/404.

    Decision tree:
      1. .mov on local disk → READY_LOCAL.
      2. .mov key in job.s3_keys → READY_R2 (caller redirects to signed URL).
      3. Mid-transcode (lock held): wait up to short_wait_seconds for it
         to land. If it lands → READY_LOCAL. Else → IN_PROGRESS with
         retry_after.
      4. Job has no umg_spec → MISCONFIGURED (400 to caller).
      5. Source MP4 not local AND not in R2 → SOURCE_MISSING (404).
      6. Otherwise → NOT_STARTED. Caller enqueues a prewarm and returns
         202 with retry_after.

    The IN_PROGRESS / NOT_STARTED retry_after values are conservative
    estimates: 30 s for "almost done", 60 s for "freshly enqueued".
    A 4K@60 cold transcode is ~90-120 s so 60 s gets a couple polls
    before reaching it.
    """
    if file_type not in FILE_MAP_PRORES:
        raise ValueError(
            f"check_prores_readiness: unsupported file_type {file_type!r}"
        )
    final_path = os.path.join(OUTPUTS_DIR, job_id, FILE_MAP_PRORES[file_type])

    # 1. local disk hit (post-prewarm or post-lazy)
    if os.path.exists(final_path):
        return ProResReadiness(ProResReadiness.READY_LOCAL, local_path=final_path)

    # 2. R2 hit (after the upload, before any new local cache)
    s3_keys = job.get("s3_keys") or {}
    if s3_keys.get(file_type):
        return ProResReadiness(ProResReadiness.READY_R2)

    # 3. Validate job is eligible BEFORE we wait or enqueue.
    if not job.get("umg_spec"):
        return ProResReadiness(
            ProResReadiness.MISCONFIGURED,
            detail="This job did not request UMG delivery; ProRes not available.",
        )

    # 4. Mid-transcode: short-wait for completion.
    if _short_wait_for_lock(job_id, file_type, max_wait_seconds=short_wait_seconds):
        return ProResReadiness(ProResReadiness.READY_LOCAL, local_path=final_path)

    # If we hit here and the lock is STILL held, the transcode is going
    # to take a while longer. Tell the caller to poll.
    if _prores_lock_for(job_id, file_type).locked():
        return ProResReadiness(
            ProResReadiness.IN_PROGRESS,
            retry_after_seconds=30,
            detail="ProRes transcode in progress; please retry shortly.",
        )

    # 5. Source MP4 missing locally AND not in R2 → can't transcode.
    source_filename, source_key_name = _SOURCE_MP4[file_type]
    source_local = os.path.join(OUTPUTS_DIR, job_id, source_filename)
    if not os.path.exists(source_local) and not s3_keys.get(source_key_name):
        return ProResReadiness(
            ProResReadiness.SOURCE_MISSING,
            detail=f"Source {source_filename} not available locally or in R2.",
        )

    # 6. Need to enqueue a prewarm. Caller does the enqueue (we don't
    # import queue_jobs here to keep the worker entrypoint dependency
    # graph tight).
    return ProResReadiness(
        ProResReadiness.NOT_STARTED,
        retry_after_seconds=60,
        detail="ProRes transcode queued; please retry in ~60 seconds.",
    )
