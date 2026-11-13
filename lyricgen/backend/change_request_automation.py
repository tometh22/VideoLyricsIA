"""Opt-in reconciliation. Uses the same proposal/apply services as the UI.

No render or publication, no fabricated human. A cutoff or explicit request
allowlist is mandatory so deploying the code never enrolls historical work.
"""
import asyncio
import os
from datetime import datetime, timezone
import logging

logger = logging.getLogger(__name__)
SYSTEM_ACTOR = {"role": "admin", "id": None, "_system_actor": True}


def request_enrolled(request):
    ids = {x.strip() for x in os.getenv('CHANGE_REQUEST_AUTOAPPLY_REQUEST_IDS', '').split(',') if x.strip()}
    if str(request.id) in ids:
        return True
    try:
        since = datetime.fromisoformat(os.environ['CHANGE_REQUEST_AUTOAPPLY_SINCE'].replace('Z', '+00:00'))
        submitted = request.submitted_at
        if since.tzinfo is None or submitted is None:
            return False
        return submitted.replace(tzinfo=submitted.tzinfo or timezone.utc) >= since
    except (KeyError, ValueError, TypeError):
        return False


async def process_request(cr_id, db, ddb):
    from database import DeliveryChangeRequest, Delivery, Job, ChangeRequestProposal
    from quality_rollout import autoapply
    import main
    from fastapi import BackgroundTasks
    cr = ddb.query(DeliveryChangeRequest).filter_by(id=cr_id).first()
    if not cr or cr.resolved_at or not request_enrolled(cr):
        return {'status': 'not_enrolled'}
    delivery = ddb.query(Delivery).filter_by(id=cr.delivery_id).first()
    job = db.query(Job).filter_by(job_id=delivery.job_id).first() if delivery else None
    if not job or not autoapply(job):
        return {'status': 'not_owned_or_enrolled'}
    # Don't regenerate a partially applied request every minute or overwrite a
    # proposal an operator adjusted. Reanalysis is explicit after intervention.
    other_requests = ddb.query(DeliveryChangeRequest).join(Delivery, Delivery.id == DeliveryChangeRequest.delivery_id).filter(
        Delivery.job_id == job.job_id, DeliveryChangeRequest.resolved_at.is_(None), DeliveryChangeRequest.id != cr_id).count()
    if other_requests:
        return {'status': 'multiple_open_requests_require_review'}
    previous = db.query(ChangeRequestProposal).filter_by(change_request_id=cr_id, portal_id=delivery.portal_id).order_by(ChangeRequestProposal.created_at.desc()).first()
    if previous is not None:
        if previous.actor_kind != 'system' or previous.status not in {'ready', 'partial'} or any(o.get('operator_adjusted') for o in previous.operations):
            return {'status': 'existing_proposal_preserved'}
        proposal = main._serialize_change_request_proposal(previous)
        if proposal['status'] == 'stale':
            if (int(previous.base_revision or 0) != int(job.segments_revision or 0)
                    or int(previous.audio_revision or 0) != int(job.audio_revision or 0)
                    or str(previous.audio_sha256 or '') != str(job.input_audio_sha256 or '')):
                return {'status': 'source_changed_requires_review'}
            proposal = main.admin_generate_change_request_proposal(
                cr_id, BackgroundTasks(), SYSTEM_ACTOR, db, ddb,
            )['proposal']
    else:
        result = main.admin_generate_change_request_proposal(
            cr_id, BackgroundTasks(), SYSTEM_ACTOR, db, ddb,
        )
        proposal = result['proposal']
    selected = [r['id'] for r in proposal['operations'] if r.get('applicable') and r.get('automatic_apply_allowed')]
    if not selected:
        return {'status': 'review_required'}
    return main.admin_apply_change_request_proposal(
        cr_id, proposal['id'],
        main.ChangeRequestProposalApply(
            base_revision=proposal['base_revision'], operation_ids=selected,
            idempotency_key='system-change-request:' + proposal['id'],
        ), SYSTEM_ACTOR, db, ddb,
    )


def reconcile_change_requests():
    from database import SessionLocal, DeliveriesSessionLocal, DeliveryChangeRequest, Delivery
    from quality_rollout import enabled
    if not enabled('CHANGE_REQUEST_AUTOAPPLY_ENABLED'):
        return
    try:
        import queue_jobs
        queue_jobs._init_redis()
        if queue_jobs._redis is None:
            return
        # Lease prevents two maintenance jobs from sharing the same scan.
        with queue_jobs._redis.lock('change-request-auto:' + os.getenv('ENVIRONMENT', ''), timeout=120, blocking_timeout=0):
            with DeliveriesSessionLocal() as ddb:
                pilot_ids = [v.strip() for v in os.getenv('QUALITY_PILOT_JOB_IDS', '').split(',') if v.strip()]
                ids = [r.id for r in ddb.query(DeliveryChangeRequest).join(Delivery, Delivery.id == DeliveryChangeRequest.delivery_id)
                       .filter(DeliveryChangeRequest.resolved_at.is_(None), Delivery.job_id.in_(pilot_ids))
                       .order_by(DeliveryChangeRequest.submitted_at.desc()).all() if request_enrolled(r)]
            for cr_id in ids:
                with SessionLocal() as db, DeliveriesSessionLocal() as ddb:
                    try:
                        asyncio.run(process_request(cr_id, db, ddb))
                    except Exception as exc:
                        db.rollback()
                        ddb.rollback()
                        logger.warning('change_request_auto request=%s error=%s', cr_id, type(exc).__name__)
    finally:
        from queue_jobs import ensure_change_request_reconciler_scheduled
        ensure_change_request_reconciler_scheduled()
