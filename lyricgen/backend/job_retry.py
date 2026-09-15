"""Public render failure state, without changing RQ's bounded retry policy."""


def render_failure_fields(rq_job, *, active_status="processing", **terminal_fields):
    """RQ 1.16 calls on_failure BEFORE decrementing retries_left.

    Unknown budgets fail closed. Stopped/cancelled jobs must never advertise a
    retry. Do not clear errors here: a late callback must not erase a terminal
    error after update_job's terminal-to-active guard rejects its status.
    """
    remaining = getattr(rq_job, "retries_left", None)
    status = getattr(rq_job, "_status", None)
    if type(remaining) is int and remaining > 0 and status not in {
        "stopped", "canceled", "cancelled", "finished", "failed",
    }:
        return {"status": active_status, "current_step": "retrying"}
    return {"status": "error", **terminal_fields}
