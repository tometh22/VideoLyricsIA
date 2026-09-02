"""Canonical job-state classification and guarded transition policy.

Keep state *classification* here. User-initiated recovery transitions still
live in their endpoints because they carry authorization and quota policy.
Background writers must use :func:`can_background_transition` so a stale
worker can never revive a closed job.
"""

from __future__ import annotations


TERMINAL_STATUSES = frozenset({
    "done",
    "pending_review",
    "error",
    "rejected",
    "validation_failed",
    "transcription_failed",
    "bg_preview_done",
    "bg_preview_failed",
})

SUCCESS_TERMINAL_STATUSES = frozenset({
    "done",
    "pending_review",
    "bg_preview_done",
})

FAILURE_TERMINAL_STATUSES = frozenset(
    TERMINAL_STATUSES - SUCCESS_TERMINAL_STATUSES
)

# Statuses that prove work is still expected. This is deliberately narrower
# than "not terminal": draft states such as awaiting_upload and
# transcribed_pending wait for a user action rather than a worker.
ACTIVE_STATUSES = frozenset({
    "queued",
    "processing",
    "editing",
    "transcribing_queued",
    "transcribing",
    "background_generating",
    "rendering",
})


def is_terminal(status: str | None) -> bool:
    return status in TERMINAL_STATUSES


def is_success_terminal(status: str | None) -> bool:
    return status in SUCCESS_TERMINAL_STATUSES


def can_background_transition(current: str | None, target: str | None) -> bool:
    """Return whether a worker/callback may apply ``current -> target``.

    Terminal-to-terminal failure callbacks are handled separately by the
    success-vs-failure guard in ``jobs.update_job``. This predicate owns the
    more dangerous terminal-to-active resurrection rule.
    """
    if target is None:
        return True
    if is_terminal(current) and not is_terminal(target):
        return False
    return True
