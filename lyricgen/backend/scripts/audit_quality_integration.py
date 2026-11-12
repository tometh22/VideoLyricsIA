#!/usr/bin/env python3
"""Read-only, tenant-private evidence export. Never activate flags or providers.

DATABASE_URL and DELIVERIES_DATABASE_URL select the inspected environments.
No credentials, signed URLs or provider payloads are printed. Save output to a
private path. Missing machine/audio/history evidence is explicit.
"""
import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def audit(job_ids):
    from sqlalchemy import text, inspect
    from sqlalchemy.orm import load_only
    from database import SessionLocal, DeliveriesSessionLocal, Job, EditorDocument, EditorVersion, Delivery, DeliveryChangeRequest, ProductEvent
    from change_request_parser import parse_change_request
    flags = ['CHANGE_REQUEST_ASSIST_ENABLED','CHANGE_REQUEST_APPLY_ENABLED','CHANGE_REQUEST_AUTOAPPLY_ENABLED',
             'QUALITY_FULL_REVIEW_ENABLED','QUALITY_PIPELINE_OBSERVE_ENABLED','QUALITY_DELIVERY_REVIEW_REQUIRED',
             'DELIVERY_QC_MODE','REFERENCE_ATTESTATION_MODE','DELIVERY_REPAIR_SHADOW_MODE','QUEUES']
    result = {'environment':os.getenv('ENVIRONMENT','unknown'), 'flags':{k:os.getenv(k,'unset') for k in flags},
              'runtime_execution_verified':False, 'jobs':[]}
    with SessionLocal() as db, DeliveriesSessionLocal() as ddb:
        for session in (db, ddb):
            if session.bind.dialect.name == 'postgresql':
                session.execute(text('SET TRANSACTION READ ONLY'))
        inspector = inspect(db.connection())
        job_columns = {c['name'] for c in inspector.get_columns('jobs')}
        result['schema'] = {
            'migration_heads': list(db.execute(text('SELECT version_num FROM alembic_version')).scalars())
                if inspector.has_table('alembic_version') else [],
            'acoustic_review_available': 'acoustic_review' in job_columns,
            'proposal_columns': [c['name'] for c in inspector.get_columns('change_request_proposals')]
                if inspector.has_table('change_request_proposals') else [],
        }
        for identity in job_ids:
            job = db.query(Job).options(load_only(*(getattr(Job,c.name) for c in Job.__table__.columns if c.name in job_columns))).filter_by(job_id=identity).first()
            if not job:
                result['jobs'].append({'job_id':identity, 'missing':['job']}); continue
            doc = db.query(EditorDocument).filter_by(job_id=identity).first()
            versions = db.query(EditorVersion).filter_by(job_id=identity).order_by(EditorVersion.revision).all()
            deliveries = ddb.query(Delivery).filter_by(job_id=identity).all()
            requests = ddb.query(DeliveryChangeRequest).filter(DeliveryChangeRequest.delivery_id.in_([d.id for d in deliveries])).all() if deliveries else []
            events = db.query(ProductEvent).filter_by(job_id=identity, name='editor_activity_heartbeat').all()
            outcomes = db.query(ProductEvent).filter(ProductEvent.job_id == identity, ProductEvent.name.in_([
                'change_request_proposal_applied', 'change_request_proposal_reverted',
                'delivery_qc_approved', 'delivery_qc_external_result'])).order_by(ProductEvent.created_at).all()
            result['jobs'].append({'job_id':identity,'artist':job.artist,'title':job.song_title,
                'audio':{'sha256':job.input_audio_sha256,'revision':job.audio_revision,'source_available':bool(job.input_r2_key)},
                'machine_evidence':doc.machine_evidence if doc else None,
                'versions':[{'revision':v.revision,'reason':v.reason,'segments':v.segments,'created_at':str(v.created_at)} for v in versions],
                'deliveries':[{'id':d.id,'portal':d.portal_id,'revision':d.published_revision,'fingerprint':d.published_render_fingerprint} for d in deliveries],
                'requests':[{'id':r.id,'comment':r.comment,'parsed':parse_change_request(r.comment),'submitted_at':str(r.submitted_at),'resolved_at':str(r.resolved_at)} for r in requests],
                'activity_events':[{'at':str(e.created_at),'properties':e.properties} for e in events],
                'outcome_events':[{'name':e.name,'at':str(e.created_at),'properties':e.properties} for e in outcomes],
                'render_count':job.edit_count,'delivery_qc':job.delivery_qc,
                'acoustic_review':job.acoustic_review if 'acoustic_review' in job_columns else None,
                'missing':[name for name,ok in [('machine_snapshot',doc and doc.machine_evidence),('audio',job.input_audio_sha256 and job.input_r2_key),('editor_history',versions),('published_revision',deliveries),('operator_activity',events)] if not ok]})
        db.rollback(); ddb.rollback()
    return result


def validate_pilot(manifest, effective_config_path=None):
    rows = manifest['masters']
    if len(rows) != 30 or sum(r['kind']=='live' for r in rows)!=15 or sum(r['kind']=='studio' for r in rows)!=15:
        raise ValueError('pilot_requires_15_live_and_15_studio')
    for field in ('job_id','audio_sha256','master_id'):
        values = [r[field] for r in rows]
        if len(set(values)) != 30 or any(not v for v in values):
            raise ValueError('pilot_identity_not_unique:' + field)
    for row in rows:
        if not re.fullmatch(r'[a-fA-F0-9]{64}', row['audio_sha256']):
            raise ValueError('pilot_audio_sha256_invalid')
    development_jobs = set(manifest.get('development_job_ids',[]))
    development_audio = set(manifest.get('development_audio_sha256',[]))
    if not development_jobs or not development_audio:
        raise ValueError('development_set_identity_required')
    if set(r['job_id'] for r in rows) & development_jobs:
        raise ValueError('development_jobs_in_evaluation')
    if set(r['audio_sha256'].lower() for r in rows) & {v.lower() for v in development_audio}:
        raise ValueError('development_audio_in_evaluation')
    if any(not r.get('kind_verified_by') for r in rows):
        raise ValueError('live_studio_classification_unverified')
    if not re.fullmatch(r'[a-fA-F0-9]{64}', str(manifest.get('frozen_config_sha256') or '')):
        raise ValueError('pilot_config_not_frozen')
    if not effective_config_path:
        raise ValueError('effective_config_file_required')
    config_sha256 = hashlib.sha256(Path(effective_config_path).read_bytes()).hexdigest()
    if config_sha256.lower() != manifest['frozen_config_sha256'].lower():
        raise ValueError('effective_config_differs_from_frozen')
    return {'valid':True,'count':30,'frozen_config_sha256':config_sha256,
            'automatic_acoustic_corrections_authorized':False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--job-ids', default='')
    parser.add_argument('--validate-pilot')
    parser.add_argument('--effective-config', help='Exact config file used for all pilot runs; hash must match frozen_config_sha256.')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    report = validate_pilot(json.loads(Path(args.validate_pilot).read_text()), args.effective_config) if args.validate_pilot else audit([x for x in args.job_ids.split(',') if x])
    target = Path(args.output)
    with target.open('x') as handle:
        os.chmod(target,0o600)
        json.dump(report,handle,ensure_ascii=False,indent=2,default=str)
    print('Report written:',target)
