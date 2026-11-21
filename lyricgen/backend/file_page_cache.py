"""Best-effort Linux page-cache advice for completed, retained files.

This never deletes or truncates a file. The return value counts bytes advised,
not bytes freed: the kernel may ignore DONTNEED, and other readers can refill
the cache immediately. Callers own the upload/persistence and lifecycle gates.
"""

import logging
import os
import stat
import sys

logger = logging.getLogger(__name__)
FileIdentity = tuple[int, int, int, int, int]


def _identity(st: os.stat_result) -> FileIdentity:
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def snapshot_file_identity(path: str) -> FileIdentity | None:
    """Capture a regular file without following a final-component symlink."""
    try:
        st = os.stat(path, follow_symlinks=False)
        return _identity(st) if stat.S_ISREG(st.st_mode) else None
    except OSError:
        return None


def release_file_page_cache(path: str, expected: FileIdentity) -> int:
    """Advise only the inode/version captured before upload; failures are benign.

    Sync this file before advising so dirty pages remain durable. The descriptor
    pins the inode if the pathname is replaced. Identity checks also skip files
    modified before/during sync, but do not replace the caller's job fencing.
    """
    if sys.platform != "linux" or not all(
        hasattr(os, name)
        for name in ("posix_fadvise", "POSIX_FADV_DONTNEED", "O_NOFOLLOW")
    ):
        return 0
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or _identity(before) != expected:
                return 0
            if before.st_size == 0:
                return 0
            os.fsync(handle.fileno())
            if _identity(os.fstat(handle.fileno())) != expected:
                return 0
            os.posix_fadvise(handle.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
            logger.info("[PAGE-CACHE] advice_requested bytes=%d", before.st_size)
            return before.st_size
    except OSError as exc:
        # Do not turn a completed upload into a failed delivery, or log paths.
        logger.warning("[PAGE-CACHE] advice skipped errno=%s", exc.errno)
        return 0
