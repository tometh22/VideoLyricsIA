"""Narrow cross-tenant access to a specifically granted campaign review."""

from __future__ import annotations

from sqlalchemy.orm import Session

from database import BatchCampaign, BatchCampaignReviewer, Job


def granted_campaign_ids(db: Session, user_id: int) -> list[str]:
    return [campaign_id for (campaign_id,) in db.query(
        BatchCampaignReviewer.campaign_id,
    ).filter(
        BatchCampaignReviewer.user_id == user_id,
        BatchCampaignReviewer.revoked_at.is_(None),
    ).all()]


def has_reviewer_grant(db: Session, campaign_id: str, user_id: int) -> bool:
    return db.query(BatchCampaignReviewer.campaign_id).filter(
        BatchCampaignReviewer.campaign_id == campaign_id,
        BatchCampaignReviewer.user_id == user_id,
        BatchCampaignReviewer.revoked_at.is_(None),
    ).first() is not None


def can_review_campaign_job(db: Session, job: Job, user: dict) -> bool:
    """A grant never opens unrelated jobs or a job moved to another tenant."""
    if user.get("role") == "admin" or job.tenant_id == user.get("tenant_id"):
        return True
    if not job.campaign_id:
        return False
    campaign = db.query(BatchCampaign).filter(
        BatchCampaign.id == job.campaign_id,
        BatchCampaign.tenant_id == job.tenant_id,
    ).first()
    return bool(campaign and has_reviewer_grant(db, campaign.id, user["id"]))
