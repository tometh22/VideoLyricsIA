"""Grant one active user access to one lyric campaign for review.

Dry-run by default. Does not move the user or campaign to another tenant.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from batch_campaigns import _scope_enabled
from database import AuditLog, BatchCampaign, BatchCampaignReviewer, SessionLocal, User


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--campaign", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    db = SessionLocal()
    try:
        campaign = db.query(BatchCampaign).filter_by(id=args.campaign).with_for_update().first()
        user = db.query(User).filter_by(username=args.username).first()
        if campaign is None or user is None or not user.is_active:
            raise SystemExit("campaign_or_active_user_not_found")
        if campaign.kind != "lyric_video" or campaign.status != "active":
            raise SystemExit("campaign_not_active_lyrics")
        if campaign.tenant_id == user.tenant_id:
            raise SystemExit("same_tenant_grant_unnecessary")
        if not _scope_enabled({
            "id": user.id, "role": user.role,
            "tenant_id": user.tenant_id, "billing_group": user.billing_group,
        }):
            raise SystemExit("reviewer_campaign_feature_disabled")
        grant = db.query(BatchCampaignReviewer).filter_by(
            campaign_id=campaign.id, user_id=user.id,
        ).with_for_update().first()
        action = "already_active" if grant and grant.revoked_at is None else "grant"
        print(json.dumps({
            "campaign_id": campaign.id, "campaign_tenant": campaign.tenant_id,
            "reviewer_id": user.id, "reviewer_username": user.username,
            "reviewer_tenant": user.tenant_id, "action": action,
            "applied": bool(args.apply and action == "grant"),
        }, ensure_ascii=False), flush=True)
        if not args.apply or action != "grant":
            db.rollback()
            return 0
        if grant is None:
            grant = BatchCampaignReviewer(campaign_id=campaign.id, user_id=user.id)
            db.add(grant)
        else:
            grant.revoked_at = None
            grant.granted_at = datetime.now(timezone.utc)
        db.add(AuditLog(user_id=None, action="campaign.reviewer_granted_by_operator", detail={
            "campaign_id": campaign.id, "reviewer_id": user.id,
            "reviewer_tenant": user.tenant_id,
        }))
        db.commit()
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
