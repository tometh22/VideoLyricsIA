"""Independent read/publish/inference controls; never infer spend from UI rollout."""
import os


def on(name):
    return os.getenv(name, "0").strip().lower() in {"1", "true", "yes", "on"}


def campaign_in_scope(campaign_id):
    allowed = os.getenv("REVIEWER_ASSIST_CAMPAIGN_ID", "").strip()
    return bool(allowed) and str(campaign_id or "") == allowed


def display_enabled(campaign_id):
    return on("REVIEWER_ASSIST_ENABLED") and campaign_in_scope(campaign_id)


def publication_enabled(campaign_id):
    return display_enabled(campaign_id) and on("REVIEWER_ASSIST_PUBLISH_ENABLED")


def inference_enabled(campaign_id):
    return display_enabled(campaign_id) and on("REVIEWER_ASSIST_INFERENCE_ENABLED")
