"""Explicit, fail-closed enrollment shared by workers, approval and publication."""
import os


def enabled(name):
    return os.getenv(name, '0').strip().lower() in {'1', 'true', 'yes', 'on'}


def enrolled(job):
    ids = {v.strip() for v in os.getenv('QUALITY_PILOT_JOB_IDS', '').split(',') if v.strip()}
    # Freeze an explicit manifest of at most 30 masters. Campaign membership
    # alone must not silently expand the paid pilot.
    return 0 < len(ids) <= 30 and str(getattr(job, 'job_id', '')) in ids


def full_review(job):
    return enabled('QUALITY_FULL_REVIEW_ENABLED') and enrolled(job)


def review_required(job):
    return enabled('QUALITY_DELIVERY_REVIEW_REQUIRED') and enrolled(job)


def autoapply(job):
    owner = os.getenv('CHANGE_REQUEST_OWNER_ENVIRONMENT', '').strip()
    return (enabled('CHANGE_REQUEST_AUTOAPPLY_ENABLED') and enrolled(job)
            and bool(owner) and owner == os.getenv('ENVIRONMENT', '').strip())
