"""Persistent identities for paid, operator-run AI generators.

Production renders already have a real ``jobs`` row. One-off scripts do not,
but AI provenance has a foreign key to that table and therefore cannot safely
accept an invented string. This module gives those scripts stable internal
jobs so provider spend is attributable and Veo's rolling ceiling can see it.
"""

from __future__ import annotations

import hashlib
import os

from database import Job, SessionLocal, User


INTERNAL_TRACKING_TENANT = "__internal_samples__"


def internal_tracking_job_id(scope: str) -> str:
    """Return a stable identifier that fits ``jobs.job_id`` (varchar(12))."""
    normalized = str(scope or "").strip()
    if not normalized:
        raise ValueError("internal tracking scope cannot be empty")
    return hashlib.sha256(f"internal-ai:{normalized}".encode()).hexdigest()[:12]


def ensure_internal_tracking_job(
    scope: str,
    *,
    db=None,
    style: str = "internal",
) -> str:
    """Create (or reuse) the real Job row required by AI provenance.

    ``INTERNAL_GENERATION_USER_ID`` may select the owner explicitly. The old
    ``MOVEMENT_SAMPLES_USER_ID`` name remains a compatibility fallback for
    the sample scripts. Without either, the first admin owns the audit row.
    Paid generation must stop when no valid owner/database exists; otherwise
    the provider call would be invisible to both attribution and cost limits.
    """
    owns_session = db is None
    if db is None:
        db = SessionLocal()
    job_id = internal_tracking_job_id(scope)
    try:
        existing = db.query(Job).filter(Job.job_id == job_id).one_or_none()
        if existing is not None:
            if existing.tenant_id != INTERNAL_TRACKING_TENANT:
                raise RuntimeError(
                    f"tracking job collision for {job_id}: "
                    f"tenant={existing.tenant_id!r}"
                )
            return job_id

        configured_owner = (
            os.environ.get("INTERNAL_GENERATION_USER_ID", "").strip()
            or os.environ.get("MOVEMENT_SAMPLES_USER_ID", "").strip()
        )
        owner = None
        if configured_owner:
            try:
                owner_id = int(configured_owner)
            except ValueError as exc:
                raise RuntimeError(
                    "INTERNAL_GENERATION_USER_ID/MOVEMENT_SAMPLES_USER_ID "
                    "must be an integer"
                ) from exc
            owner = db.query(User).filter(User.id == owner_id).one_or_none()
        else:
            owner = (
                db.query(User)
                .filter(User.role == "admin")
                .order_by(User.id)
                .first()
            )
        if owner is None:
            raise RuntimeError(
                "No valid owner for internal AI tracking; set "
                "INTERNAL_GENERATION_USER_ID to an existing user"
            )

        safe_scope = str(scope).strip()[:450]
        db.add(Job(
            job_id=job_id,
            user_id=owner.id,
            tenant_id=INTERNAL_TRACKING_TENANT,
            artist="",
            song_title="",
            style=str(style or "internal")[:50],
            filename=f"internal-{safe_scope}",
            status="internal_sample",
            current_step="sample",
            progress=100,
        ))
        db.commit()
        return job_id
    except Exception:
        db.rollback()
        raise
    finally:
        if owns_session:
            db.close()
