"""Prueba prospectiva del aire mínimo antes de la línea siguiente.

Brazo A = comportamiento actual (sin aire extra); brazo B =
``LYRIC_MIN_GAP_AB_ARM_B_MS`` (300 ms). La asignación alterna por job, en el
orden en que los jobs llegan a transcribirse, y queda en el AuditLog
(``experiment.lyric_min_gap.assigned``), que es append-only: un reintento o
una re-transcripción del mismo job reusa su brazo.

Solo entran jobs nuevos de campañas UMG (batch, sin aprobar, sin documento
del editor ni revisiones, fuera de pilotos y de tenants de CI). Un job ya
aprobado nunca se asigna, así que la prueba no toca nada aprobado.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger("genly.line_gap_experiment")

ASSIGNED = "experiment.lyric_min_gap.assigned"
EXPOSURE = "experiment.lyric_min_gap.exposure"
COHORT = "lyric-min-gap-ab-v1"
# Clave fija del advisory lock que serializa la alternancia entre workers.
_LOCK_KEY = 0x4C4D47414231  # "LMGAB1"
_TRUE = {"1", "true", "yes", "on"}


def enabled() -> bool:
    return os.environ.get("LYRIC_MIN_GAP_AB_ENABLED", "0").strip().lower() in _TRUE


def arm_b_ms() -> int:
    try:
        return max(0, int(float(os.environ.get("LYRIC_MIN_GAP_AB_ARM_B_MS", "300"))))
    except (TypeError, ValueError):
        return 300


def max_jobs() -> int:
    try:
        return max(0, int(os.environ.get("LYRIC_MIN_GAP_AB_MAX_JOBS", "100")))
    except (TypeError, ValueError):
        return 100


def eligible(job, *, has_document: bool) -> bool:
    from cost_attribution import is_ci_tenant, is_umg_tenant

    tenant = str(getattr(job, "tenant_id", "") or "")
    return bool(
        str(getattr(job, "workload_class", "") or "").lower() == "batch"
        and getattr(job, "campaign_id", None)
        and getattr(job, "campaign_item_id", None)
        and getattr(job, "approved_at", None) is None
        and int(getattr(job, "segments_revision", 0) or 0) == 0
        and getattr(job, "pilot_id", None) is None
        and not has_document
        and is_umg_tenant(tenant)
        and not is_ci_tenant(tenant)
    )


def _existing(db, job_id: str):
    from database import AuditLog

    return (
        db.query(AuditLog)
        .filter(AuditLog.action == ASSIGNED, AuditLog.detail["job_id"].as_string() == job_id)
        .order_by(AuditLog.id.asc())
        .first()
    )


def assignment_for(db, job_id: str) -> dict | None:
    row = _existing(db, job_id)
    return dict(row.detail) if row is not None and isinstance(row.detail, dict) else None


def assign(db, job) -> dict | None:
    """Devuelve ``{"arm", "gap_ms", ...}`` o ``None`` si el job no participa.

    Un job ya asignado conserva su brazo aunque la prueba se haya apagado
    después: un reintento no puede cambiar el tratamiento a mitad de camino.
    """
    from database import AuditLog, EditorDocument

    job_id = str(job.job_id)
    previous = assignment_for(db, job_id)
    if previous is not None:
        return previous
    if not enabled():
        return None
    has_document = db.query(EditorDocument.job_id).filter(
        EditorDocument.job_id == job_id,
    ).first() is not None
    if not eligible(job, has_document=has_document):
        return None
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        from sqlalchemy import text

        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _LOCK_KEY})
        previous = assignment_for(db, job_id)
        if previous is not None:
            return previous
    assigned = db.query(AuditLog.id).filter(AuditLog.action == ASSIGNED).count()
    if assigned >= max_jobs():
        return None
    arm = "A" if assigned % 2 == 0 else "B"
    detail = {
        "job_id": job_id,
        "cohort": COHORT,
        "ordinal": assigned + 1,
        "arm": arm,
        "gap_ms": arm_b_ms() if arm == "B" else 0,
        "tenant_id": str(job.tenant_id or ""),
        "campaign_id": str(job.campaign_id or ""),
        "assigned_at": datetime.now(timezone.utc).isoformat(),
    }
    db.add(AuditLog(user_id=None, action=ASSIGNED, detail=detail))
    db.commit()
    logger.info("[LINE_GAP_AB] job=%s brazo=%s gap=%sms (#%s)",
                job_id, arm, detail["gap_ms"], detail["ordinal"])
    return detail


def record_exposure(db, job_id: str, assignment: dict, stats: dict) -> None:
    """Cuántas líneas tocó el tratamiento (B) o habría podido tocar (A)."""
    from database import AuditLog

    db.add(AuditLog(user_id=None, action=EXPOSURE, detail={
        "job_id": job_id,
        "cohort": COHORT,
        "arm": assignment.get("arm"),
        "gap_ms": assignment.get("gap_ms"),
        **{key: int(stats.get(key, 0)) for key in
           ("polish_calls", "lines_seen", "lines_trimmed", "lines_word_floor")},
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }))
    db.commit()
