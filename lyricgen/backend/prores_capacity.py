"""Host-wide ProRes admission: strict queue priority and a shared encode slot.

Portal deliveries (UMG) and staging pre-renders are both ffmpeg ProRes
jobs. They used to share the ``enterprise`` queue with ordinary renders,
and a file lock taken *inside* an already dequeued job only saw workers
blocked on that lock. Jobs still sitting in RQ were invisible, so a
prewarm could occupy every worker while a portal export waited in the
queue. A stalled R2 download held the same slot until the RQ timeout.

The fix is three separate mechanisms:

* Dedicated queues, consumed in order ``prores_portal`` then
  ``prores_prewarm``. RQ's multi-queue BLPOP already prefers the earlier
  queue. Customer clicks (``force=True`` and portal materialize) go to
  the portal queue; background prewarm goes to the other one.
* A dedicated worker never dequeues an encode until a slot is free, and
  never dequeues prewarm while any deployment has published portal
  demand. The worker stays idle. The job stays in Redis, where the
  priority decision can see it.
* Source bytes are downloaded, with an inactivity timeout, a total cap
  and bounded retries, *before* that slot is taken.

Unset ``PRORES_PORTAL_QUEUE`` / ``PRORES_PREWARM_QUEUE`` keep the legacy
``enterprise`` queue, so a Railway deploy that does not set them behaves
as before. ``PRORES_MAX_CONCURRENCY`` unset means unlimited encodes.

The semaphore is a directory of ``flock`` files. Staging and production
have separate Redis instances; the lock directory is the shared host
resource. ``PRORES_SEMAPHORE_NAME`` selects the subdirectory so two
unrelated pairs on one machine do not share a cap.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass

logger = logging.getLogger("genly.prores.capacity")
_legacy_cap_warned = False

LEGACY_QUEUE = "enterprise"
_RESERVED_QUEUES = frozenset({LEGACY_QUEUE, "default"})
_DEFAULT_INACTIVITY_SECONDS = 45.0
_DEFAULT_TOTAL_SECONDS = 420.0
_DEFAULT_ATTEMPTS = 2
_MARKER_TTL_GRACE_SECONDS = 180.0


class ProResDefer(Exception):
    """Encode slot is busy. The worker must requeue this job at the front.

    Raising is deliberate: the job must not block inside ``flock``. The
    dedicated worker treats this as "inputs are ready, try again when a
    slot is free" and does not count it as a failure.
    """

    def __init__(self, message: str = "ProRes encode slot busy"):
        super().__init__(message)


class DownloadTimeout(Exception):
    """A ProRes source download stalled or exceeded its wall-clock cap."""

    def __init__(self, reason: str):
        if reason not in ("inactivity", "total"):
            reason = "total"
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class Admission:
    """What a ProRes worker should do on one look at the queues.

    ``occupies_worker`` is False for ``wait``: the job stays queued and
    the process does not sit inside a lock. That is the property the
    in-job flock did not have.
    """

    action: str  # pass, wait, download, encode
    queue: str | None  # portal, prewarm, or None
    takes_slot: bool
    occupies_worker: bool


def portal_queue_name() -> str:
    return os.environ.get("PRORES_PORTAL_QUEUE", "").strip() or LEGACY_QUEUE


def prewarm_queue_name() -> str:
    return os.environ.get("PRORES_PREWARM_QUEUE", "").strip() or LEGACY_QUEUE


def coordination_enabled() -> bool:
    """True when both ProRes classes have left the shared render queues."""
    return (
        portal_queue_name() not in _RESERVED_QUEUES
        and prewarm_queue_name() not in _RESERVED_QUEUES
    )


def strict_queue_order() -> list[str]:
    """Portal first. A single legacy queue stays a single entry."""
    portal, prewarm = portal_queue_name(), prewarm_queue_name()
    if portal == prewarm:
        return [portal]
    return [portal, prewarm]


def dedicated_queue_names() -> list[str]:
    if not coordination_enabled():
        return []
    return strict_queue_order()


def gate_enabled_for(queue_names: list[str]) -> bool:
    """A worker may apply the gate only when it has no other queues.

    A render worker that still listens to ``enterprise`` and ``default``
    must not refuse those jobs because a ProRes marker exists.
    """
    if not coordination_enabled():
        return False
    names = [name for name in queue_names if name]
    allowed = set(strict_queue_order())
    return bool(names) and set(names) <= allowed


def max_concurrency() -> int | None:
    """None means unlimited: today's behaviour when the env var is unset."""
    raw = os.environ.get("PRORES_MAX_CONCURRENCY", "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "[PRORES] ignoring invalid PRORES_MAX_CONCURRENCY=%r; unlimited",
            raw,
        )
        return None
    if value <= 0:
        return None
    return value


def download_inactivity_seconds() -> float:
    return _positive_float(
        "PRORES_DOWNLOAD_INACTIVITY_SECONDS", _DEFAULT_INACTIVITY_SECONDS,
    )


def download_total_seconds() -> float:
    return _positive_float(
        "PRORES_DOWNLOAD_TOTAL_SECONDS", _DEFAULT_TOTAL_SECONDS,
    )


def download_max_attempts() -> int:
    raw = os.environ.get("PRORES_DOWNLOAD_MAX_ATTEMPTS", "").strip()
    if not raw:
        return _DEFAULT_ATTEMPTS
    try:
        return max(1, int(raw))
    except ValueError:
        return _DEFAULT_ATTEMPTS


def _positive_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def decide_admission(
    *,
    gate_enabled: bool,
    portal_queued: int,
    prewarm_queued: int,
    host_portal_waiting: int,
    portal_head_ready: bool,
    prewarm_head_ready: bool,
    concurrency_limit: int | None,
    slots_free: int | None,
) -> Admission:
    """Pick the next action. Portal demand always beats prewarm.

    ``host_portal_waiting`` counts marker files published by *any*
    deployment using this semaphore name, including the other stack's
    Redis. A prewarm job is not dequeued while that count is non-zero,
    even if this process's own portal queue is empty.

    ``slots_free`` is ``None`` when the caller has not measured the lock
    (unlimited, or it will try the lock itself). ``0`` means every encode
    slot is taken: an encode-ready job stays queued instead of occupying
    a worker.
    """
    if not gate_enabled:
        return Admission("pass", None, False, False)
    if portal_queued > 0:
        target, ready = "portal", portal_head_ready
    elif host_portal_waiting > 0:
        return Admission("wait", None, False, False)
    elif prewarm_queued > 0:
        target, ready = "prewarm", prewarm_head_ready
    else:
        return Admission("wait", None, False, False)
    if not ready:
        # Download does not take an encode slot. The worker is busy only
        # for the bounded transfer, then the job goes back to the queue
        # if the CPU slot is not free.
        return Admission("download", target, False, True)
    limited = concurrency_limit is not None
    if limited and slots_free is not None and slots_free <= 0:
        return Admission("wait", target, False, False)
    return Admission("encode", target, limited, True)


def _safe_token(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return (cleaned or "prores")[:180]


def semaphore_root() -> str:
    base = os.environ.get("PRORES_SEMAPHORE_DIR", "/var/lock/genly-prores").strip()
    base = base or "/var/lock/genly-prores"
    name = _safe_token(os.environ.get("PRORES_SEMAPHORE_NAME", "prores").strip() or "prores")
    return os.path.join(base, name)


def _mkdir_shared(path: str) -> None:
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, 0o777)
    except OSError:
        pass


class HeldSlot:
    """One exclusive encode permit. Closing the fd releases the flock."""

    def __init__(self, fd):
        self._fd = fd

    def release(self) -> None:
        fd = self._fd
        self._fd = None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            fd.close()
        except OSError:
            pass


def try_acquire_slot() -> HeldSlot | None:
    """Non-blocking. None means every slot is taken or the directory is unusable.

    Callers must not sleep on this. A waiting job belongs back in RQ.
    """
    limit = max_concurrency()
    if limit is None:
        return HeldSlot(None)
    root = os.path.join(semaphore_root(), "slots")
    try:
        _mkdir_shared(root)
    except OSError as exc:
        logger.error("[PRORES] cannot create semaphore dir %s: %s", root, exc)
        return None
    for index in range(limit):
        path = os.path.join(root, f"slot-{index}")
        try:
            fd = open(path, "a+")
        except OSError as exc:
            logger.error("[PRORES] cannot open semaphore slot %s: %s", path, exc)
            return None
        try:
            os.chmod(path, 0o666)
        except OSError:
            pass
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fd.close()
            continue
        except OSError:
            fd.close()
            continue
        return HeldSlot(fd)
    return None


def _demand_dir() -> str:
    return os.path.join(semaphore_root(), "portal-demand")


def _marker_ttl() -> float:
    raw = os.environ.get("PRORES_PREWARM_TIMEOUT_SECONDS", "900").strip() or "900"
    try:
        timeout = float(raw)
    except ValueError:
        timeout = 900.0
    return max(60.0, timeout) + _MARKER_TTL_GRACE_SECONDS


def mark_portal_demand(job_id: str) -> None:
    """Publish that a customer ProRes job is waiting, for every stack on this host."""
    if not coordination_enabled() or not job_id:
        return
    directory = _demand_dir()
    try:
        _mkdir_shared(directory)
        path = os.path.join(directory, _safe_token(job_id))
        with open(path, "w", encoding="ascii") as handle:
            handle.write(str(time.time()))
        try:
            os.chmod(path, 0o666)
        except OSError:
            pass
    except OSError as exc:
        logger.warning("[PRORES] portal demand marker not written for %s: %s", job_id, exc)


def clear_portal_demand(job_id: str) -> None:
    if not job_id:
        return
    path = os.path.join(_demand_dir(), _safe_token(job_id))
    try:
        os.unlink(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning("[PRORES] could not clear portal demand %s: %s", job_id, exc)


def refresh_portal_markers(job_ids) -> None:
    """Keep markers alive while the portal job is still queued."""
    if not coordination_enabled():
        return
    now = str(time.time())
    directory = _demand_dir()
    try:
        _mkdir_shared(directory)
    except OSError:
        return
    for job_id in list(job_ids)[:100]:
        if not job_id:
            continue
        path = os.path.join(directory, _safe_token(str(job_id)))
        try:
            with open(path, "w", encoding="ascii") as handle:
                handle.write(now)
        except OSError:
            continue


def host_portal_waiting() -> int:
    """Marker files newer than the ProRes job timeout, across both deployments."""
    directory = _demand_dir()
    if not os.path.isdir(directory):
        return 0
    ttl = _marker_ttl()
    now = time.time()
    waiting = 0
    try:
        names = os.listdir(directory)
    except OSError:
        return 0
    for name in names:
        path = os.path.join(directory, name)
        try:
            with open(path, "r", encoding="ascii") as handle:
                stamped = float((handle.read() or "0").strip() or "0")
        except (OSError, ValueError):
            try:
                stamped = os.path.getmtime(path)
            except OSError:
                continue
        if now - stamped > ttl:
            try:
                os.unlink(path)
            except OSError:
                pass
            continue
        waiting += 1
    return waiting


def _current_rq_job():
    try:
        from rq import get_current_job
        return get_current_job()
    except Exception:
        return None


def abandon_rq_retries() -> None:
    """Stop RQ from running this job again.

    A download that already used its inactivity and total budget must not
    be multiplied by ``Retry(max=2)``. Three passes of two 420 s attempts
    is about 42 minutes of a portal job sitting in front of the host.
    """
    job = _current_rq_job()
    if job is None:
        return
    try:
        job.retries_left = 0
        job.save()
    except Exception:
        logger.warning("[PRORES] could not clear RQ retries", exc_info=True)


def clear_current_portal_demand() -> None:
    job = _current_rq_job()
    if job is not None:
        clear_portal_demand(getattr(job, "id", "") or "")


def remember_inputs(path: str) -> None:
    """Remember a finished download on the RQ job so the next dequeue skips it."""
    job = _current_rq_job()
    if job is None or not path:
        return
    meta = dict(getattr(job, "meta", None) or {})
    meta["prores_inputs_ready"] = True
    meta["prores_input_path"] = path
    job.meta = meta
    try:
        job.save_meta()
    except Exception:
        logger.warning("[PRORES] could not persist downloaded input path", exc_info=True)


def rq_job_inputs_ready(job) -> bool:
    meta = getattr(job, "meta", None) or {}
    path = meta.get("prores_input_path")
    if not meta.get("prores_inputs_ready") or not path:
        return False
    try:
        return os.path.getsize(path) > 0
    except OSError:
        return False


def _warn_legacy_cap() -> None:
    global _legacy_cap_warned
    if _legacy_cap_warned or max_concurrency() is None:
        return
    _legacy_cap_warned = True
    logger.warning(
        "[PRORES] PRORES_MAX_CONCURRENCY=%s ignored on the legacy enterprise "
        "queue. Set PRORES_PORTAL_QUEUE=prores_portal and "
        "PRORES_PREWARM_QUEUE=prores_prewarm.",
        max_concurrency(),
    )


@contextlib.contextmanager
def release_portal_demand_on_terminal():
    """Drop the host marker when the job finishes.

    A failure keeps the marker. RQ may retry the same id, and prewarm on
    the other deployment must keep waiting. The marker expires with the
    ProRes job timeout if nobody clears it. ``ProResDefer`` is a requeue,
    not a finish.
    """
    try:
        yield
    except BaseException:
        raise
    else:
        clear_current_portal_demand()


@contextlib.contextmanager
def transcode_slot():
    """Hold the host encode slot around ffmpeg only.

    When coordination is off this is a no-op, including when
    ``PRORES_MAX_CONCURRENCY`` is set on a legacy enterprise worker.
    Applying the cap there would defer jobs back onto a worker that does
    not understand the gate, and the worker would spin. The cap is
    enforced by dedicated ProRes workers.
    """
    if not coordination_enabled():
        _warn_legacy_cap()
        yield
        return
    limit = max_concurrency()
    if limit is None or os.environ.get("PRORES_SLOT_HELD") == "1":
        # Encode is starting (or there is no cap). This job is no longer
        # "waiting", so prewarm on the other deployment may use a free slot.
        clear_current_portal_demand()
        yield
        return
    slot = try_acquire_slot()
    if slot is None:
        raise ProResDefer("ProRes encode slot busy")
    clear_current_portal_demand()
    try:
        yield
    finally:
        slot.release()


def copy_bounded(read, write, *, inactivity: float, total: float) -> None:
    """Copy until EOF. ``read(n, timeout)`` raises TimeoutError if it stalled.

    A TimeoutError means the reader waited for the full ``timeout`` it was
    given. The binding budget (inactivity versus total) selects the reason,
    so a slow-but-steady 65 KB/s stream still dies on the total cap and a
    socket that stops returning bytes dies on inactivity.
    """
    if total <= 0:
        raise DownloadTimeout("total")
    if inactivity <= 0:
        raise DownloadTimeout("inactivity")
    start = time.monotonic()
    last = start
    while True:
        now = time.monotonic()
        elapsed = now - start
        quiet = now - last
        if elapsed >= total:
            raise DownloadTimeout("total")
        if quiet >= inactivity:
            raise DownloadTimeout("inactivity")
        budget = min(inactivity - quiet, total - elapsed)
        if budget <= 0:
            raise DownloadTimeout("total" if (total - elapsed) <= (inactivity - quiet) else "inactivity")
        try:
            chunk = read(1024 * 1024, budget)
        except TimeoutError:
            limited_by_inactivity = (inactivity - quiet) <= (total - elapsed) + 1e-9
            raise DownloadTimeout("inactivity" if limited_by_inactivity else "total") from None
        if not chunk:
            return
        write(chunk)
        last = time.monotonic()


def run_attempts(operation, attempts: int) -> None:
    """Run ``operation`` up to ``attempts`` times. Only timeouts are retried."""
    last: DownloadTimeout | None = None
    for _ in range(max(1, int(attempts))):
        try:
            operation()
            return
        except DownloadTimeout as exc:
            last = exc
    assert last is not None
    raise last


def cached_input_path(key: str, etag: str | None = None) -> str:
    """Stable path for one object version.

    The ETag is part of the identity. A correction overwrites the same R2
    key with new bytes; keying only by the key would transcode the previous
    cut and the later ETag check would still pass, because it reads the
    current remote object.
    """
    root = os.environ.get("PRORES_INPUT_CACHE", "/tmp/genly-prores-inputs").strip()
    root = root or "/tmp/genly-prores-inputs"
    material = key if not etag else f"{key}\0{etag}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return os.path.join(root, digest, "source.bin")


def discard_cached_input(key: str, etag: str | None = None) -> None:
    path = cached_input_path(key, etag)
    try:
        os.unlink(path)
    except FileNotFoundError:
        return
    except OSError:
        logger.warning("[PRORES] could not remove cached input %s", path, exc_info=True)


def download_prores_source(key: str, dest: str) -> bool:
    """Download a ProRes source before any encode slot is taken.

    Returns False when the object is missing or the transfer fails. Callers
    raise their own domain error. When R2 is not configured this calls
    ``storage.download_object`` so existing fakes keep working. Timeouts are
    bounded so a stalled object cannot sit until the RQ job timeout.
    """
    import storage

    if not key:
        return False
    # Tests and dev replace storage.download_object and leave R2 unset.
    # Production transfers go through the bounded reader below.
    if not storage.is_enabled():
        try:
            return bool(storage.download_object(key, dest))
        except TypeError:
            return bool(storage.download_object(key, dest))
    try:
        _download_r2(key, dest)
    except DownloadTimeout as exc:
        # The attempts inside this job are the whole budget. RQ Retry would
        # start them over.
        abandon_rq_retries()
        logger.warning(
            "[PRORES] source download stopped key=%s reason=%s after %s attempt(s)",
            key, exc.reason, download_max_attempts(),
        )
        return False
    except Exception:
        logger.exception("[PRORES] source download failed key=%s", key)
        return False
    return True


def _download_r2(key: str, dest: str) -> None:
    import storage

    inactivity = download_inactivity_seconds()
    total = download_total_seconds()
    partial = dest + ".partial"

    def once() -> None:
        body = _open_r2_body(storage, key, read_timeout=max(inactivity, 1.0))
        try:
            parent = os.path.dirname(dest)
            if parent:
                os.makedirs(parent, exist_ok=True)
            if os.path.exists(partial):
                os.unlink(partial)

            def read(size: int, budget: float):
                return _read_body(body, size, budget)

            with open(partial, "wb") as handle:
                copy_bounded(
                    read, handle.write, inactivity=inactivity, total=total,
                )
            os.replace(partial, dest)
        finally:
            try:
                body.close()
            except Exception:
                pass

    try:
        run_attempts(once, download_max_attempts())
    finally:
        if os.path.exists(partial):
            try:
                os.unlink(partial)
            except OSError:
                pass


def _open_r2_body(storage, key: str, *, read_timeout: float):
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError

    client = boto3.client(
        "s3",
        endpoint_url=storage.R2_ENDPOINT_URL,
        aws_access_key_id=storage.R2_ACCESS_KEY_ID,
        aws_secret_access_key=storage.R2_SECRET_ACCESS_KEY,
        config=Config(
            signature_version="s3v4",
            connect_timeout=10,
            read_timeout=max(read_timeout, 1.0),
            retries={"max_attempts": 1},
        ),
    )
    try:
        response = client.get_object(Bucket=storage.R2_BUCKET, Key=key)
    except ClientError as exc:
        code = (exc.response or {}).get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            raise FileNotFoundError(key) from exc
        raise
    return response["Body"]


def _read_body(body, size: int, budget: float):
    """Read one chunk, or raise TimeoutError if ``budget`` elapses with no bytes."""
    import threading

    box: dict = {}

    def _pull():
        try:
            box["chunk"] = body.read(size)
        except Exception as exc:
            box["err"] = exc

    thread = threading.Thread(target=_pull, name="prores-source-read", daemon=True)
    thread.start()
    thread.join(max(budget, 0.001))
    if thread.is_alive():
        try:
            body.close()
        except Exception:
            pass
        raise TimeoutError("prores source read stalled")
    if "err" in box:
        exc = box["err"]
        name = type(exc).__name__
        if "Timeout" in name or isinstance(exc, TimeoutError):
            raise TimeoutError(str(exc)) from exc
        raise exc
    return box.get("chunk") or b""
