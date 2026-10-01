"""Read-only audit of the exact objects promised by portal publications.

A newer working candidate is not a broken immutable publication. Missing,
unknown, malformed and unchecked artifacts must never be reported as healthy.
"""
from __future__ import annotations

import logging
import os
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone

import storage
from delivery_retention import DELIVERY_FILENAMES

logger = logging.getLogger(__name__)
STALE_IN_FLIGHT_HOURS = int(os.environ.get("DELIVERY_STALE_ALERT_HOURS", "24"))
MAX_OBJECT_CHECKS = int(os.environ.get("DELIVERY_AUDIT_MAX_CHECKS", "2000"))
AUDIT_TIMEOUT_SECONDS = max(0.0, float(os.environ.get("DELIVERY_AUDIT_TIMEOUT_SECONDS", "12")))
# One bounded pool across passes: an unfinished timed-out HEAD must not create
# four more threads on every subsequent audit. No ORM objects enter this pool.
_OBJECT_AUDIT_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="delivery-audit")


def _inspect_budgeted(checks, *, deadline):
    """Return completed outcomes without waiting for stragglers at deadline.

    Running network calls cannot be killed safely. Their client has short
    connect/read timeouts; the shared executor bounds outstanding concurrency.
    Results that arrive after the deadline are not retroactively called green.
    """
    remaining = iter(checks)
    pending = {}
    results = []
    attempted = 0

    def inspect(check):
        try:
            return storage.object_status_bounded(check[2])
        except Exception:
            return "unavailable"

    def fill():
        nonlocal attempted
        while len(pending) < 4 and time.monotonic() < deadline:
            try:
                check = next(remaining)
            except StopIteration:
                break
            pending[_OBJECT_AUDIT_POOL.submit(inspect, check)] = check
            attempted += 1

    fill()
    while pending:
        left = deadline - time.monotonic()
        if left <= 0:
            break
        done, _ = wait(pending, timeout=left, return_when=FIRST_COMPLETED)
        if not done or time.monotonic() >= deadline:
            break
        for future in done:
            check = pending.pop(future)
            try:
                status = future.result()
            except Exception:
                status = "unavailable"
            results.append((check, status))
        fill()
    cancelled = 0
    for future, check in pending.items():
        if future.cancel():
            cancelled += 1
        else:
            results.append((check, "deadline_exceeded"))
    unchecked = len(checks) - attempted + cancelled
    return results, attempted - cancelled, unchecked


def _key(tenant: str, job_id: str, file_type: str) -> str | None:
    filename = DELIVERY_FILENAMES.get(file_type)
    if not filename:
        return None
    return (f"{storage._safe_filename(tenant)}/{storage._safe_filename(job_id)}"
            f"/{storage._safe_filename(filename)}")


def _published_key(row: dict, file_type: str) -> tuple[str | None, str | None]:
    """Match the portal's fail-closed snapshot semantics; never fall back."""
    keys = row.get("published_file_keys")
    if keys is not None:
        if not isinstance(keys, dict):
            return None, "invalid_snapshot_manifest"
        key = keys.get(file_type)
        if not isinstance(key, str) or not key.strip():
            return None, "missing_snapshot_key"
        return key, None
    key = _key(row["tenant"], row["job_id"], file_type)
    return (key, None) if key else (None, "unsupported_file_type")


def _budgeted_checks(checks: list, *, now: datetime, cursor: int | None = None):
    """Fair contiguous windows over a stable catalogue, even after restart.

    The default rotates each UTC day; ceil(N/budget) consecutive daily runs
    cover a stable catalogue. Explicit cursor/next_cursor permit additional
    passes in the same day. Unselected objects remain explicitly unchecked.
    """
    count, budget = len(checks), max(0, MAX_OBJECT_CHECKS)
    if not count or not budget:
        return [], 0
    day = int(now.timestamp() // 86400)
    start = (cursor if cursor is not None else day * budget) % count
    selected = [checks[(start + i) % count] for i in range(min(budget, count))]
    return selected, (start + len(selected)) % count


def _finding(row, file_type=None, reason=None):
    result = {"delivery_id": row["id"], "portal_id": row["portal_id"],
              "job_id": row["job_id"], "song": f"{row['artist']} — {row['song']}"}
    if file_type is not None:
        result["file_type"] = file_type
    if reason is not None:
        result["reason"] = reason
    return result


def audit_active_deliveries(*, now: datetime | None = None, cursor: int | None = None) -> dict:
    """Report only. A failure must not kill the reaper or become a green run."""
    report = {
        "checked": 0, "objects_checked": 0, "objects_verified": 0,
        "objects_total": 0, "objects_unchecked": 0, "next_cursor": 0,
        "phantom": [], "outdated": [], "candidate_available": [],
        "in_flight_too_long": [], "on_demand": [], "unknown": [],
        "manifest_failures": [], "unevaluable": 0, "complete": False,
    }
    started_at = time.monotonic()
    deadline = started_at + AUDIT_TIMEOUT_SECONDS
    try:
        from database import Delivery, Job, SessionLocal, scoped_deliveries_db
        from delivery_freshness import _aware, render_fingerprint

        now = _aware(now or datetime.now(timezone.utc))
        cutoff = now - timedelta(hours=STALE_IN_FLIGHT_HOURS)
        with scoped_deliveries_db() as ddb:
            rows = (ddb.query(Delivery).filter(Delivery.removed_at.is_(None))
                    .order_by(Delivery.id.asc()).all())
            snapshot = [{
                "id": d.id, "job_id": d.job_id, "portal_id": d.portal_id or "argentina",
                "tenant": d.tenant_snapshot, "artist": d.artist_snapshot,
                "song": d.song_title_snapshot, "file_types": list(d.file_types or []),
                "fingerprint": d.published_render_fingerprint,
                "published_file_keys": d.published_file_keys,
                "stale_since": d.stale_since, "stale_reason": d.stale_reason,
            } for d in rows]
        report["checked"] = len(snapshot)
        by_id = {row["id"]: row for row in snapshot}
        checks = []
        for row in snapshot:
            if not row["file_types"]:
                report["manifest_failures"].append(_finding(row, reason="empty_file_types"))
            for ft in sorted(set(row["file_types"])):
                report["objects_total"] += 1
                key, problem = _published_key(row, ft)
                if problem:
                    report["manifest_failures"].append(_finding(row, ft, problem))
                else:
                    checks.append((row["id"], ft, key))
        selected, report["next_cursor"] = _budgeted_checks(checks, now=now, cursor=cursor)
        report["cursor_mode"] = "explicit" if cursor is not None else "utc_day_rotation"
        if not storage.is_enabled():
            selected = []
            report["storage_status"] = "unavailable"
        else:
            report["storage_status"] = "enabled"
        report["objects_unchecked"] = len(checks) - len(selected)
        # Our deliveries session AND the reaper caller's work/lock sessions
        # are closed before this remote I/O (see reaper's deferred audit).
        results, attempted, unchecked = _inspect_budgeted(selected, deadline=deadline)
        report["objects_attempted"] = attempted
        report["objects_unchecked"] += unchecked
        report["objects_checked"] = sum(status != "deadline_exceeded" for _, status in results)
        report["deadline_exceeded"] = bool(unchecked or any(status == "deadline_exceeded" for _, status in results))
        for (did, ft, _key), status in results:
            row = by_id[did]
            if status == "exists":
                report["objects_verified"] += 1
            elif status == "missing":
                report["phantom"].append(_finding(row, ft, "object_missing"))
            else:
                report["unknown"].append(_finding(row, ft,
                    "deadline_exceeded" if status == "deadline_exceeded" else "storage_unavailable"))

        job_ids = [row["job_id"] for row in snapshot if row["fingerprint"]]
        jobs = {}
        if job_ids:
            with SessionLocal() as db:
                for job in db.query(Job).filter(Job.job_id.in_(job_ids)).all():
                    jobs[(job.tenant_id, job.job_id)] = render_fingerprint(job)
        for row in snapshot:
            current = jobs.get((row["tenant"], row["job_id"]))
            if not row["fingerprint"] or current is None:
                report["unevaluable"] += 1
            elif current != row["fingerprint"]:
                # Only a legacy mutable pointer drifts when working bytes change.
                category = "candidate_available" if row["published_file_keys"] is not None else "outdated"
                report[category].append(_finding(row))
            since = _aware(row["stale_since"])
            if since and since < cutoff:
                item = _finding(row, reason=row["stale_reason"])
                item["since"] = since.isoformat()
                report["in_flight_too_long"].append(item)
        report["complete"] = not (report["objects_unchecked"] or report["unknown"])
    except Exception as exc:
        logger.warning("[DELIVERY-AUDIT] auditoría incompleta: %s", type(exc).__name__)
        report["error"] = type(exc).__name__
    report["elapsed_seconds"] = round(time.monotonic() - started_at, 3)
    return report


def _increment_audit_metrics(report: dict) -> None:
    try:
        from ops_metrics import increment
        increment("delivery_audit_runs", 1)
        increment("delivery_audit_error", 1 if report.get("error") else 0)
        for metric, field in (("phantom", "phantom"), ("outdated", "outdated"),
                              ("in_flight", "in_flight_too_long"), ("on_demand", "on_demand"),
                              ("unknown", "unknown"), ("manifest", "manifest_failures"),
                              ("candidate", "candidate_available")):
            increment("delivery_audit_" + metric, len(report.get(field, [])))
        increment("delivery_audit_unchecked", report.get("objects_unchecked", 0))
    except Exception:
        pass


def log_audit(report: dict) -> None:
    _increment_audit_metrics(report)
    if report.get("error"):
        logger.error("[DELIVERY-AUDIT] incompleta, NO es un resultado: %s", report["error"])
        return
    if report.get("checked") and not report.get("objects_checked"):
        logger.error("[DELIVERY-AUDIT] %d entregas y CERO objetos chequeados; no es una auditoría sana",
                     report["checked"])
    if report.get("unknown") or report.get("objects_unchecked"):
        logger.warning("[DELIVERY-AUDIT] incompleta: %d desconocidos, %d sin comprobar",
                       len(report.get("unknown", [])), report.get("objects_unchecked", 0))
    for category in ("phantom", "outdated", "manifest_failures"):
        for item in report.get(category, []):
            logger.error("[DELIVERY-AUDIT] %s entrega=%s portal=%s tipo=%s motivo=%s",
                         category, item["delivery_id"], item["portal_id"],
                         item.get("file_type"), item.get("reason"))
    for item in report.get("in_flight_too_long", []):
        logger.warning("[DELIVERY-AUDIT] EN VUELO entrega=%s motivo=%s desde=%s",
                       item["delivery_id"], item.get("reason"), item["since"])
    problems = any(report.get(field) for field in
                   ("phantom", "outdated", "manifest_failures", "in_flight_too_long"))
    if report.get("complete") and not problems:
        logger.info("[DELIVERY-AUDIT] archivos comprobados sin fallos: %d; "
                    "candidatos posteriores=%d, frescura no evaluable=%d",
                    report.get("objects_verified", 0), len(report.get("candidate_available", [])),
                    report.get("unevaluable", 0))
