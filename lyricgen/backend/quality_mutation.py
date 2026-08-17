"""Single fail-closed authorization gate for automatic lyric mutations."""
from __future__ import annotations


def _tenant_for_job(job_id: str) -> str:
    """Resolve the persisted tenant without trusting caller-supplied metadata."""
    if not job_id:
        return ""
    try:
        from database import Job, SessionLocal

        db = SessionLocal()
        try:
            row = db.query(Job.tenant_id).filter(Job.job_id == job_id).first()
            if not row:
                return ""
            value = row[0] if isinstance(row, tuple) else getattr(row, "tenant_id", None)
            return str(value or "")
        finally:
            db.close()
    except Exception:
        return ""


def mutation_authorized(*, job_id: str, tenant_id: str | None = None) -> bool:
    """Require signed calibration and membership in the effective enforce cohort.

    Tenant identity is loaded from the persisted job when omitted, so a pilot
    tenant configured with a zero percentage rollout is still recognized.  Any
    failure in calibration or policy evaluation declines the mutation.
    """
    try:
        from transcription_quality import calibration_identity, effective_policy_mode

        if not calibration_identity().get("calibrated"):
            return False
        persisted_tenant = _tenant_for_job(job_id) if tenant_id is None else str(tenant_id)
        return effective_policy_mode(
            job_id=str(job_id or ""), tenant_id=persisted_tenant,
        ) == "enforce"
    except Exception:
        return False
