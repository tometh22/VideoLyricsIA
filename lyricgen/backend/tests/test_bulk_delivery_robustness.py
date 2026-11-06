"""A bulk campaign send must fail per song, never as a whole, and be retryable."""
import pytest
from fastapi import HTTPException

import art_track_campaigns as atc
from database import AuditLog, DeliveryBatch, DeliveryBatchItem, Job, SessionLocal
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
    assert atc.process_delivery_batch(op) == {'sent': 1, 'failed': 1}
    items = _items(op)
    assert items[first][:2] == ('failed', 'unexpected_error')
    assert items[second][0] == 'sent'   # one broken song never stops the ones after it
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


def _age(op, seconds):
    from datetime import datetime, timedelta, timezone
    with SessionLocal() as db:
        operation = db.get(DeliveryBatch, op)
        operation.updated_at = datetime.now(timezone.utc) - timedelta(seconds=seconds)
        db.commit()


def test_sweeper_requeues_a_stalled_operation_once(client, admin_token, monkeypatch, storage_ready):
    _, first, second, op = _two_song_operation(client, admin_token, 'sweep')
    queued = []
    monkeypatch.setattr(atc, 'enqueue_delivery_batch', lambda operation_id: (queued.append(operation_id), True)[1])
    monkeypatch.setattr(atc, '_delivery_batch_rq_state', lambda operation_id: None)
    with SessionLocal() as db:
        assert atc.reconcile_stalled_delivery_batches(db) == 0  # fresh: leave it alone
        assert queued == []
    _age(op, atc.STALL_AFTER_SECONDS + 60)
    with SessionLocal() as db:
        assert atc.reconcile_stalled_delivery_batches(db) == 1
        assert queued == [op]
        assert atc.reconcile_stalled_delivery_batches(db) == 0  # updated_at was refreshed
        assert db.query(AuditLog).filter_by(action='delivery.batch_requeued').count() == 1


def test_sweeper_never_duplicates_a_live_worker(client, admin_token, monkeypatch, storage_ready):
    _, first, second, op = _two_song_operation(client, admin_token, 'live')
    queued = []
    monkeypatch.setattr(atc, 'enqueue_delivery_batch', lambda operation_id: (queued.append(operation_id), True)[1])
    monkeypatch.setattr(atc, '_delivery_batch_rq_state', lambda operation_id: 'started')
    _age(op, atc.STALL_AFTER_SECONDS + 60)
    with SessionLocal() as db:
        assert atc.reconcile_stalled_delivery_batches(db) == 0
    assert queued == []


def test_a_stalled_operation_is_reported_to_the_client(client, admin_token, storage_ready):
    _, first, second, op = _two_song_operation(client, admin_token, 'flag')
    auth = {'Authorization': f'Bearer {admin_token}'}
    assert client.get(f'/batch/delivery-operations/{op}', headers=auth).json()['stalled'] is False
    _age(op, atc.STALL_AFTER_SECONDS + 60)
    assert client.get(f'/batch/delivery-operations/{op}', headers=auth).json()['stalled'] is True


def test_pointer_mode_bulk_send_copies_nothing_and_leaves_the_pointer_null(client, admin_token, monkeypatch, storage_ready):
    from database import Delivery
    monkeypatch.setenv('PUBLISH_LATEST_POINTER', '1')
    def forbidden(*args, **kwargs):
        raise AssertionError('pointer mode must not copy files')
    monkeypatch.setattr('delivery_snapshots.copy_snapshot', forbidden)
    _, first, second, op = _two_song_operation(client, admin_token, 'pointer')
    assert atc.process_delivery_batch(op) == {'sent': 2, 'failed': 0}
    with SessionLocal() as db:
        rows = db.query(Delivery).filter(Delivery.job_id.in_([first, second])).all()
        assert len(rows) == 2 and all(row.published_file_keys is None for row in rows)


def test_a_failure_marked_earlier_survives_a_later_crash(client, admin_token, monkeypatch, storage_ready):
    """A `continue`-path failure (stale approval) is uncommitted when the next song
    raises; the rollback of that song must not turn it back into 'pending'."""
    _, first, second, op = _two_song_operation(client, admin_token, 'survives')
    with SessionLocal() as db:
        rows = db.query(DeliveryBatchItem).filter_by(delivery_batch_id=op).order_by(DeliveryBatchItem.created_at, DeliveryBatchItem.id).all()
        ordered = [r.job_id for r in rows]
        stale = db.query(Job).filter_by(job_id=ordered[0]).one()
        stale.render_params = {**(stale.render_params or {}), 'edited_after_approval': True}  # changes the fingerprint
        db.commit()
    real = atc.delivery_freshness.render_fingerprint

    def boom(job):
        if job.job_id == ordered[1]:
            raise RuntimeError('synthetic outage')
        return real(job)

    monkeypatch.setattr(atc.delivery_freshness, 'render_fingerprint', boom)
    atc.process_delivery_batch(op)
    items = _items(op)
    assert items[ordered[0]][:2] == ('failed', 'stale_approval')
    assert items[ordered[1]][:2] == ('failed', 'unexpected_error')


def test_items_are_processed_in_a_stable_order(client, admin_token, monkeypatch, storage_ready):
    _, first, second, op = _two_song_operation(client, admin_token, 'ordered')
    seen = []
    real = atc.delivery_freshness.render_fingerprint
    monkeypatch.setattr(atc.delivery_freshness, 'render_fingerprint', lambda job: (seen.append(job.job_id), real(job))[1])
    atc.process_delivery_batch(op)
    with SessionLocal() as db:
        expected = [r.job_id for r in db.query(DeliveryBatchItem).filter_by(delivery_batch_id=op).order_by(DeliveryBatchItem.created_at, DeliveryBatchItem.id)]
    assert [j for j in dict.fromkeys(seen)] == expected


def _mark(op, job_id, status, code=None, operation_status=None):
    with SessionLocal() as db:
        row = db.query(DeliveryBatchItem).filter_by(delivery_batch_id=op, job_id=job_id).one()
        row.status = status; row.error_code = code
        if operation_status:
            db.get(DeliveryBatch, op).status = operation_status
        db.commit()


def test_a_stale_approval_is_not_offered_a_retry_that_can_never_work(client, admin_token, monkeypatch, storage_ready):
    _, first, second, op = _two_song_operation(client, admin_token, 'stale-no-retry')
    auth = {'Authorization': f'Bearer {admin_token}'}
    monkeypatch.setattr(atc, 'enqueue_delivery_batch', lambda operation_id: True)
    monkeypatch.setattr(atc, '_delivery_batch_rq_state', lambda operation_id: None)
    _mark(op, first, 'failed', 'stale_approval', 'partial')
    _mark(op, second, 'sent')
    body = client.get(f'/batch/delivery-operations/{op}', headers=auth).json()
    assert next(i for i in body['items'] if i['job_id'] == first)['retryable'] is False
    refused = client.post(f'/batch/delivery-operations/{op}/retry', headers=auth)
    assert refused.status_code == 409 and refused.json()['detail']['code'] == 'nothing_to_retry'
    _mark(op, first, 'failed', 'deliverables_not_ready')   # a retryable failure does get one
    assert client.post(f'/batch/delivery-operations/{op}/retry', headers=auth).status_code == 202


def test_an_unreachable_redis_is_never_read_as_no_job(client, admin_token, monkeypatch, storage_ready):
    _, first, second, op = _two_song_operation(client, admin_token, 'redis-down')
    auth = {'Authorization': f'Bearer {admin_token}'}
    queued = []
    monkeypatch.setattr(atc, 'enqueue_delivery_batch', lambda operation_id: (queued.append(operation_id), True)[1])
    monkeypatch.setattr(atc, '_delivery_batch_rq_state', lambda operation_id: 'unknown')
    response = client.post(f'/batch/delivery-operations/{op}/retry', headers=auth)
    assert response.status_code == 202 and response.json()['outcome'] == 'unavailable'
    assert queued == []


def test_the_sweeper_moves_on_from_operations_it_cannot_requeue(client, admin_token, monkeypatch, storage_ready):
    _, first, second, op = _two_song_operation(client, admin_token, 'sweep-bump')
    monkeypatch.setattr(atc, 'enqueue_delivery_batch', lambda operation_id: True)
    monkeypatch.setattr(atc, '_delivery_batch_rq_state', lambda operation_id: 'started')
    _age(op, atc.STALL_AFTER_SECONDS + 60)
    with SessionLocal() as db:
        assert atc.reconcile_stalled_delivery_batches(db) == 0
    body = client.get(f'/batch/delivery-operations/{op}', headers={'Authorization': f'Bearer {admin_token}'}).json()
    assert body['stalled'] is False   # looked at once; it will be considered again in 10 minutes


def test_a_send_cannot_carry_an_unbounded_list_of_ticks(client, admin_token, monkeypatch, storage_ready):
    monkeypatch.setenv('CAMPAIGN_CHANGE_REQUEST_ACTIONS', '1')
    campaign = _campaign_for(client, admin_token, 'caps')
    job_id = _seed_campaign_job(campaign, umg_spec=None, s3_keys=dict(KEYS))
    response = client.post(f'/batch/campaigns/{campaign.id}/deliveries', headers={'Authorization': f'Bearer {admin_token}'},
                           json={'job_ids': [job_id], 'destination_portal': 'chile', 'idempotency_key': 'caps-operation-key-1',
                                 'resolve_requests': {f'j{n}': [n] for n in range(201)}})
    assert response.status_code == 422 and response.json()['detail'] == {'code': 'too_many_change_requests'}
