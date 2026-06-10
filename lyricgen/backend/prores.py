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


class ProResSuperseded(Exception):
    """The job was re-rendered (edited) WHILE this transcode was running, so
    the .mov we produced is from a stale source. We discard it instead of
    publishing — the edit's own re-enqueued prewarm regenerates from the new
    cut. See the freshness fence in ensure_prores_exists."""


def _source_identity(source_path: str):
    """(st_size, st_mtime_ns) of the source MP4, or (None, None) if absent.

    This is the MOST DIRECT freshness signal: the .mov is a pure function of
    this file, and every re-render (edit OR /retry) rewrites lyric_video.mp4
    in place, bumping size/mtime. Catches paths that don't move the DB
    signals — notably /retry, which resets edit_count to 0 and leaves
    editing_started_at None (so the DB part alone is blind to it)."""
    try:
        st = os.stat(source_path)
        return (st.st_size, st.st_mtime_ns)
    except OSError:
        return (None, None)


def _render_fingerprint(job_id: str, source_path: str | None = None):
    """Freshness signal for the transcode fence in ensure_prores_exists:
    (edit_count, editing_started_at, source_size, source_mtime_ns).

    - edit_count: typography/background/lyrics edits.
    - editing_started_at: metadata edits (which deliberately do NOT bump
      edit_count, see main.py:request_edit) — they still re-render the title
      card, so their .mov must not be served stale.
    - source size+mtime: the direct "the bytes I'm transcoding changed"
      signal, and the ONLY one that catches /retry (edit_count reset to 0,
      editing_started_at None — identical DB tuple before and after a retry).

    The DB part is read fresh from Postgres each call. Comparing the whole
    tuple before vs after the transcode tells us whether the render this .mov
    derives from is still current."""
    from database import SessionLocal, Job
    db = SessionLocal()
    try:
        row = db.query(Job).filter(Job.job_id == job_id).first()
        db_part = (None, None) if row is None else (row.edit_count or 0, row.editing_started_at)
    finally:
        db.close()
    size, mtime_ns = _source_identity(source_path) if source_path else (None, None)
    return (db_part[0], db_part[1], size, mtime_ns)


def _is_superseded(job_id: str, source_path: str, baseline) -> bool:
    """True only if we can POSITIVELY prove the render moved since `baseline`
    (the fingerprint snapshotted before the transcode).

    A transient Postgres blip during the post-transcode recheck must NOT
    discard a good 60-300 s transcode: `_render_fingerprint` reads the DB and
    would otherwise propagate the error out of `ensure_prores_exists`, failing
    the prewarm (which has no RQ retry). On any read failure we return False
    ("can't prove superseded → proceed") — correctness is still guarded by the
    edit's remove_s3_keys + the freshness short-circuit on the next download."""
    try:
        return _render_fingerprint(job_id, source_path) != baseline
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("[PRORES] %s fingerprint recheck failed (%s); proceeding",
                       job_id, e)
        return False


def _mov_is_fresh(mov_path: str, source_path: str) -> bool:
    """A cached .mov is fresh iff it is at least as new as the source MP4 it
    derives from. A re-render (edit/retry) rewrites the source in place,
    pushing the source mtime above the old .mov's — so `mov older than source`
    means the .mov is a stale pre-render cut and must be re-transcoded.

    If the source isn't local we can't compare: return True (status quo —
    the s3_keys/upload fences cover the R2 publish path)."""
    try:
        if not os.path.exists(source_path):
            return True
        return os.stat(mov_path).st_mtime_ns >= os.stat(source_path).st_mtime_ns
    except OSError:
        return True


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
    source_filename, source_key_name = _SOURCE_MP4[file_type]
    source_path = os.path.join(OUTPUTS_DIR, job_id, source_filename)

    # Short-circuit ONLY on a FRESH cached .mov. A .mov older than its source
    # MP4 means the source was re-rendered (edit/retry) after the .mov was
    # built — returning it would hand back the pre-edit cut. Fall through to
    # re-transcode in that case. (Freshness audit 2026-06-09.)
    if os.path.exists(file_path) and _mov_is_fresh(file_path, source_path):
        return file_path

    umg_spec = job.get("umg_spec")
    if not umg_spec:
        raise ProResMisconfigured(
            "This job did not request UMG delivery; ProRes not available."
        )

    lock = _prores_lock_for(job_id, file_type)
    with lock:
        # Double-check inside the lock: a sibling caller may have
        # finished a FRESH transcode while we were waiting.
        if os.path.exists(file_path) and _mov_is_fresh(file_path, source_path):
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
        # Freshness fence (audit 2026-06-09): the source lyric_video.mp4 is
        # MUTABLE — run_edit_pipeline overwrites it in place on an edit. This
        # transcode runs 60-300 s in a SEPARATE process (the per-(job,type)
        # lock above is a threading.Lock, in-process only — it does NOT
        # serialize against the edit worker). If an edit lands mid-transcode,
        # the .mov we built is the PRE-edit cut; publishing it (os.replace +
        # merge_s3_keys below) would re-create the stale .mov and re-add the
        # stale R2 key AFTER run_edit_pipeline invalidated them — silently
        # reproducing the original stale-ProRes bug for UMG. We snapshot a
        # render fingerprint before transcoding and re-check it just before
        # publishing; if it moved, we discard the .mov and raise
        # ProResSuperseded — the edit's own re-enqueued prewarm regenerates
        # from the new cut.
        fingerprint = _render_fingerprint(job_id, source_path)

        # ffmpeg writes to .tmp; we rename atomically once the post-
        # transcode validator is happy. Two processes may race on the
        # same source but only one's os.replace lands. The loser's .tmp
        # is overwritten or unlinked below.
        tmp_path = f"{file_path}.tmp"
        try:
            _transcode_to_prores(source_path, tmp_path, spec)
            if _is_superseded(job_id, source_path, fingerprint):
                raise ProResSuperseded(
                    f"Job {job_id} re-rendered during {file_type} transcode; "
                    "discarding stale .mov (fingerprint changed)."
                )
            os.replace(tmp_path, file_path)
        finally:
            # If transcode raised mid-way (or we discarded a superseded
            # build), drop the partial.
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        # Best-effort R2 upload so future downloads of this ProRes skip
        # the transcode entirely. Don't fail the caller if it errors.
        # (Unreachable when superseded at os.replace — the raise above skips
        # this.)
        try:
            if storage.is_enabled():
                key = storage.upload_master(
                    file_path, tenant_id, job_id, FILE_MAP_PRORES[file_type],
                )
                if key:
                    # SECOND freshness fence (audit 2026-06-09): the upload
                    # above takes seconds (a 4K master is GBs). An edit/retry
                    # can land in that window — os.replace published a then-
                    # fresh .mov, but it is stale NOW. Re-check before merging
                    # the key: otherwise merge_s3_keys would re-add a key
                    # pointing at the pre-edit bytes AFTER run_edit_pipeline's
                    # remove_s3_keys already dropped it (last-writer-wins
                    # across separate FOR-UPDATE txns). On supersede we drop our
                    # now-stale LOCAL .mov and bail WITHOUT merging the key.
                    #
                    # We deliberately do NOT storage.delete_object(key): the R2
                    # key is fully deterministic ({tenant}/{job}/umg_master.mov),
                    # so the edit's re-enqueued prewarm — running on another
                    # replica — uploads the FRESH master to the SAME key. A
                    # blind delete here can race-clobber that fresh object,
                    # leaving s3_keys pointing at a deleted key → permanent 404
                    # (worse than a stale cut; audit re-review 2026-06-09). A
                    # stale object briefly left at the key is harmless: it is
                    # overwritten by the fresh upload (last-writer), and we
                    # never merged a key pointing at it.
                    if _is_superseded(job_id, source_path, fingerprint):
                        logger.warning(
                            "[PRORES] %s/%s superseded during R2 upload; "
                            "dropping stale local .mov, not merging key",
                            job_id, file_type,
                        )
                        try:
                            os.unlink(file_path)
                        except OSError:
                            pass
                        raise ProResSuperseded(
                            f"Job {job_id} re-rendered during {file_type} R2 upload."
                        )
                    # U1 fix (audit 2026-05-25): merge atómico vía
                    # `jobs.merge_s3_keys` — SELECT FOR UPDATE + read +
                    # write en una sola tx. El path anterior (read en tx
                    # A, write en tx B) tenía una race window entre los
                    # dos prewarm workers (umg_master + umg_short) que
                    # corrieron en paralelo: ambos snapshotean s3_keys={}
                    # antes del transcode de 60-300s, y al final el
                    # segundo en escribir pisa la key del primero. Prod
                    # 2026-05-12: 8/18 jobs UMG perdieron una key,
                    # reconciliado a mano por SQL.
                    from jobs import merge_s3_keys
                    merge_s3_keys(job_id, file_type, key)
        except ProResSuperseded:
            raise
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
    # Observability 2026-06-10: toda línea de log de este job lleva job_id.
    from observability import set_job_log_context
    set_job_log_context(job_id)
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
    except (ProResMisconfigured, ProResSourceMissing, ProResSuperseded) as e:
        # These are normal "this job is not eligible / no longer current for
        # ProRes prewarm" outcomes — log and exit cleanly without raising,
        # so the RQ job ends in `finished` not `failed`. ProResSuperseded
        # specifically means an edit raced us; the edit re-enqueued its own
        # prewarm, which will produce the fresh .mov.
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
    source_filename, source_key_name = _SOURCE_MP4[file_type]
    source_local = os.path.join(OUTPUTS_DIR, job_id, source_filename)

    # 1. local disk hit (post-prewarm or post-lazy) — but only serve a FRESH
    # .mov. A .mov older than the local source MP4 is a stale pre-edit cut
    # (e.g. a straggler from before the freshness fences); fall through so we
    # re-transcode rather than 200 the wrong master. Mirrors the short-circuit
    # in ensure_prores_exists.
    if os.path.exists(final_path) and _mov_is_fresh(final_path, source_local):
        return ProResReadiness(ProResReadiness.READY_LOCAL, local_path=final_path)

    # 2. R2 hit (after the upload, before any new local cache). The key is
    # only merged after the second freshness fence in ensure_prores_exists,
    # so a present key points at a fresh master.
    s3_keys = job.get("s3_keys") or {}
    if s3_keys.get(file_type):
        return ProResReadiness(ProResReadiness.READY_R2)

    # 3. Validate job is eligible BEFORE we wait or enqueue.
    if not job.get("umg_spec"):
        return ProResReadiness(
            ProResReadiness.MISCONFIGURED,
            detail="This job did not request UMG delivery; ProRes not available.",
        )

    # 4. Mid-transcode: short-wait for completion. Same freshness contract as
    # step 1 — only serve the just-landed .mov if it's at least as new as the
    # source (a completing transcode produces a fresh file; the guard is
    # defense-in-depth so this path can never 200 a stale cut).
    if (_short_wait_for_lock(job_id, file_type, max_wait_seconds=short_wait_seconds)
            and _mov_is_fresh(final_path, source_local)):
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


def scan_stale_prores(limit: int = 300) -> list[dict]:
    """Detecta masters ProRes más viejos que su MP4 fuente (drift de cache).

    Guardia post-incidente 2026-06-10: los edits previos al fix de
    invalidación (#622) dejaron 15 masters stale latentes que solo se
    descubrieron cuando una operadora de universal_argentina descargó
    uno. Este scan es la versión automática de la auditoría manual de
    ese día: si un .mov en R2 es más viejo que el lyric_video.mp4 del
    que debería derivar, la descarga va a servir un cut viejo.

    Solo LECTURA (head_object x2 por job con ambos keys). El remediador
    es scripts/fix_stale_prores.py --apply --rewarm por cada job_id
    reportado.

    Returns:
        Lista de dicts {job_id, tenant_id, lag_seconds} — vacía si todo
        está fresco.
    """
    from database import SessionLocal, Job
    import storage as _storage

    if not _storage.is_enabled():
        return []
    client = _storage._get_client()
    if client is None:
        return []

    stale: list[dict] = []
    db = SessionLocal()
    try:
        jobs = (
            db.query(Job)
            .filter(Job.s3_keys.isnot(None))
            .order_by(Job.created_at.desc())
            .limit(limit)
            .all()
        )
        for j in jobs:
            s3 = j.s3_keys or {}
            master_key = s3.get("umg_master")
            video_key = s3.get("video")
            if not master_key or not video_key:
                continue
            try:
                m = client.head_object(Bucket=_storage.R2_BUCKET, Key=master_key)["LastModified"]
                v = client.head_object(Bucket=_storage.R2_BUCKET, Key=video_key)["LastModified"]
            except Exception:
                # Objeto borrado/permiso/red — no es señal de staleness.
                continue
            if m < v:
                stale.append({
                    "job_id": j.job_id,
                    "tenant_id": j.tenant_id,
                    "lag_seconds": int((v - m).total_seconds()),
                })
    finally:
        db.close()

    if stale:
        logger.warning(
            "[PRORES][STALE-SCAN] %d master(s) más viejos que su MP4: %s",
            len(stale), ", ".join(s["job_id"] for s in stale),
        )
        # Sentry con fingerprint estable (estilo #630): UN issue agrupado
        # cuyo event count crece — las alert rules disparan por frecuencia.
        try:
            import sentry_sdk
            with sentry_sdk.push_scope() as _scope:
                _scope.fingerprint = ["stale-prores-scan"]
                _scope.set_extra("stale_jobs", stale[:50])
                _scope.set_extra("count", len(stale))
                sentry_sdk.capture_message(
                    f"[PRORES][STALE-SCAN] {len(stale)} master(s) stale — "
                    "remediar con scripts/fix_stale_prores.py",
                    level="error",
                )
        except Exception:
            pass
    return stale
