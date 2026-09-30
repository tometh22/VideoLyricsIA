"""A bulk campaign send must fail per song, never as a whole, and be retryable."""
import pytest
from fastapi import HTTPException

import art_track_campaigns as atc
from database import AuditLog, DeliveryBatch, DeliveryBatchItem, SessionLocal
from tests.test_art_track_campaigns import (  # noqa: F401  (autouse fixtures)
    _campaign_for, _run_bulk_delivery, _seed_campaign_job, clean_art_rows, publication_storage,
)

KEYS = {'video': 't/j/lyric_video.mp4', 'short': 't/j/short.mp4', 'thumbnail': 't/j/thumbnail.jpg'}


@pytest.fixture
def storage_ready(monkeypatch):
    monkeypatch.setenv('BATCH_CAMPAIGN_ENABLED', '1')
    monkeypatch.setattr('delivery_snapshots.copy_snapshot', lambda tenant, jid, kinds: {k: 'pinned/' + k for k in kinds})
    monkeypatch.setattr(atc.storage, 'is_enabled', lambda: True)
    monkeypatch.setattr(atc.storage, 'object_exists', lambda key: not key.endswith('.mov'))


def _two_song_operation(client, admin_token, name):
    campaign = _campaign_for(client, admin_token, name)
    first = _seed_campaign_job(campaign, umg_spec=None, s3_keys=dict(KEYS))
    second = _seed_campaign_job(campaign, umg_spec=None, s3_keys=dict(KEYS))
    return campaign, first, second, _run_bulk_delivery(client, admin_token, campaign.id, 'robustness-op-' + name)


def _items(operation_id):
    with SessionLocal() as db:
        return {row.job_id: (row.status, row.error_code, row.error_detail)
                for row in db.query(DeliveryBatchItem).filter_by(delivery_batch_id=operation_id)}


def test_ambiguous_replacement_fails_that_song_only(client, admin_token, monkeypatch, storage_ready):
    _, first, second, op = _two_song_operation(client, admin_token, 'ambiguous')
    import delivery_replacement
    real = delivery_replacement.target

    def target(db, ddb, job, portal):
        if job.job_id == first:
            raise HTTPException(status_code=409, detail='Hay varios pedidos vinculados a esta variante.')
        return real(db, ddb, job, portal)

    monkeypatch.setattr(delivery_replacement, 'target', target)
    assert atc.process_delivery_batch(op) == {'sent': 1, 'failed': 1}
    items = _items(op)
    assert items[first][:2] == ('failed', 'ambiguous_replacement')
    assert 'varios pedidos' in items[first][2]
    assert items[second][0] == 'sent'
    with SessionLocal() as db:
        assert db.get(DeliveryBatch, op).status == 'partial'


def test_unexpected_error_does_not_leave_the_operation_sending(client, admin_token, monkeypatch, storage_ready):
    _, first, second, op = _two_song_operation(client, admin_token, 'unexpected')
    real = atc.delivery_freshness.render_fingerprint

    def boom(job):
        if job.job_id == first:
            raise RuntimeError('synthetic outage')
        return real(job)

    monkeypatch.setattr(atc.delivery_freshness, 'render_fingerprint', boom)
    atc.process_delivery_batch(op)
    items = _items(op)
    assert items[first][:2] == ('failed', 'unexpected_error')
    with SessionLocal() as db:
        operation = db.get(DeliveryBatch, op)
        assert operation.status == 'partial'  # retryable, not stuck in "sending"
        assert operation.completed_at is None


def test_each_sent_song_leaves_an_audit_entry(client, admin_token, storage_ready):
    _, first, second, op = _two_song_operation(client, admin_token, 'audit')
    assert atc.process_delivery_batch(op) == {'sent': 2, 'failed': 0}
    with SessionLocal() as db:
        rows = [r for r in db.query(AuditLog).filter(AuditLog.action.in_(('delivery.create', 'delivery.update')))
                if (r.detail or {}).get('delivery_batch_id') == op]
    assert {r.detail['job_id'] for r in rows} == {first, second}
    assert all(r.detail['source'] == 'campaign_bulk' and r.detail['portal_id'] == 'chile' for r in rows)


def test_backend_enforces_the_campaign_portal(client, admin_token, monkeypatch):
    monkeypatch.setenv('BATCH_CAMPAIGN_ENABLED', '1')
    created = client.post('/batch/campaigns', headers={'Authorization': f'Bearer {admin_token}'},
                          json={'name': 'Locked', 'expected_count': 1, 'kind': 'lyric_video',
                                'destination_portal': 'argentina'})
    assert created.status_code == 200, created.text
    response = client.post(f"/batch/campaigns/{created.json()['id']}/deliveries",
                           headers={'Authorization': f'Bearer {admin_token}'},
                           json={'destination_portal': 'chile', 'idempotency_key': 'locked-portal-key'})
    assert response.status_code == 409, response.text
    assert response.json()['detail'] == {'code': 'portal_locked', 'portal': 'argentina'}


def test_operation_reports_error_detail_and_retryability(client, admin_token, monkeypatch, storage_ready):
    _, first, second, op = _two_song_operation(client, admin_token, 'report')
    import delivery_replacement
    real = delivery_replacement.target
    monkeypatch.setattr(delivery_replacement, 'target', lambda db, ddb, job, portal: (
        (_ for _ in ()).throw(HTTPException(status_code=409, detail='ambiguo')) if job.job_id == first
        else real(db, ddb, job, portal)))
    atc.process_delivery_batch(op)
    body = client.get(f'/batch/delivery-operations/{op}', headers={'Authorization': f'Bearer {admin_token}'}).json()
    failed = next(item for item in body['items'] if item['job_id'] == first)
    assert failed['error_code'] == 'ambiguous_replacement' and failed['error_detail'] == 'ambiguo'
    assert failed['retryable'] is False
    assert body['stalled'] is False


def test_retry_requeues_only_when_nothing_is_running(client, admin_token, monkeypatch, storage_ready):
    _, first, second, op = _two_song_operation(client, admin_token, 'retry')
    auth = {'Authorization': f'Bearer {admin_token}'}
    queued = []
    monkeypatch.setattr(atc, 'enqueue_delivery_batch', lambda operation_id: (queued.append(operation_id), True)[1])
    monkeypatch.setattr(atc, '_delivery_batch_rq_state', lambda operation_id: 'started')
    running = client.post(f'/batch/delivery-operations/{op}/retry', headers=auth)
    assert running.status_code == 202 and running.json()['outcome'] == 'already_running'
    assert queued == []
    monkeypatch.setattr(atc, '_delivery_batch_rq_state', lambda operation_id: None)
    again = client.post(f'/batch/delivery-operations/{op}/retry', headers=auth)
    assert again.status_code == 202 and again.json()['outcome'] == 'queued'
    assert queued == [op]
    with SessionLocal() as db:
        assert db.query(AuditLog).filter_by(action='delivery.batch_retry').count() == 2


def test_retry_refuses_completed_and_fresh_sending_operations(client, admin_token, monkeypatch, storage_ready):
    _, first, second, op = _two_song_operation(client, admin_token, 'refuse')
    auth = {'Authorization': f'Bearer {admin_token}'}
    monkeypatch.setattr(atc, 'enqueue_delivery_batch', lambda operation_id: True)
    monkeypatch.setattr(atc, '_delivery_batch_rq_state', lambda operation_id: None)
    with SessionLocal() as db:
        operation = db.get(DeliveryBatch, op)
        operation.status = 'sending'
        db.commit()
    busy = client.post(f'/batch/delivery-operations/{op}/retry', headers=auth)
    assert busy.status_code == 409 and busy.json()['detail']['code'] == 'operation_in_progress'
    atc.process_delivery_batch(op)
    done = client.post(f'/batch/delivery-operations/{op}/retry', headers=auth)
    assert done.status_code == 409 and done.json()['detail']['code'] == 'nothing_to_retry'
