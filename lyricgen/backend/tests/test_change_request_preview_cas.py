"""Real-DB preview fencing: confirm only the proposal the operator saw."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from time import monotonic

import pytest

from database import DeliveryChangeRequest, EditorDocument
from tests.conftest import auth
from tests.test_deliveries import (
    approved_job, all_r2_files_present, _publication_copy_succeeds,
)


@pytest.fixture
def proposal_case(client, admin_token, approved_job, db, all_r2_files_present, monkeypatch):
    monkeypatch.setenv('CHANGE_REQUEST_ASSIST_ENABLED', '1')
    monkeypatch.setenv('CHANGE_REQUEST_APPLY_ENABLED', '1')
    # Do not reach any real worker/model during the API integration test.
    monkeypatch.setattr('main._dispatch_editor_quality_outbox', lambda *_: None)
    approved_job.segments_json = [{'start': 0, 'end': 3, 'text': 'Viejo'}]
    approved_job.segments_revision = 0
    db.commit()
    response = client.post('/admin/deliveries/from-job/' + approved_job.job_id,
                           headers=auth(admin_token), json={})
    assert response.status_code == 200, response.text
    request = DeliveryChangeRequest(delivery_id=response.json()['delivery_id'],
                                    comment='0:01: "Hola, mundo!"')
    db.add(request); db.commit()
    base = f'/admin/change-requests/{request.id}/proposals'
    response = client.post(base, headers=auth(admin_token))
    assert response.status_code == 200, response.text
    proposal = response.json()['proposal']
    assert len(proposal['content_hash']) == 64
    return base + '/' + proposal['id'], proposal, approved_job.job_id


def _body(proposal, **extra):
    return {'base_revision': proposal['base_revision'],
            'expected_proposal_hash': proposal['content_hash'],
            'operation_ids': [op['id'] for op in proposal['operations'] if op.get('applicable')],
            'idempotency_key': 'preview-cas-test-intention', **extra}


def test_concurrent_proposal_adjustment_invalidates_seen_preview(client, admin_token, proposal_case, db):
    path, seen, job_id = proposal_case
    headers = auth(admin_token)
    patch = {'base_revision': seen['base_revision'], 'expected_proposal_hash': seen['content_hash'],
             'operation_id': _body(seen)['operation_ids'][0], 'requested_text': 'Otro texto revisado'}
    changed = client.patch(path, headers=headers, json=patch)
    assert changed.status_code == 200, changed.text
    assert changed.json()['proposal']['content_hash'] != seen['content_hash']
    stale_patch = client.patch(path, headers=headers, json={**patch, 'requested_text': 'No debe ganar'})
    assert stale_patch.status_code == 409, stale_patch.text
    stale_apply = client.post(path + '/apply', headers=headers, json=_body(seen))
    assert stale_apply.status_code == 409, stale_apply.text
    assert stale_apply.json()['detail']['code'] == 'proposal_preview_changed'
    db.expire_all()
    doc = db.query(EditorDocument).filter_by(job_id=job_id).one()
    assert doc.revision == 0 and doc.current_segments[0]['text'] == 'Viejo'
    fresh = changed.json()['proposal']
    applied = client.post(path + '/apply', headers=headers, json=_body(fresh))
    assert applied.status_code == 200, applied.text
    db.expire_all()
    assert doc.current_segments[0]['text'] == 'Otro texto revisado'


def test_apply_requires_preview_hash_and_binds_replay_payload(client, admin_token, proposal_case, db):
    path, proposal, _ = proposal_case
    body = _body(proposal)
    missing = {key: value for key, value in body.items() if key != 'expected_proposal_hash'}
    assert client.post(path + '/apply', headers=auth(admin_token), json=missing).status_code == 409
    first = client.post(path + '/apply', headers=auth(admin_token), json=body)
    assert first.status_code == 200, first.text
    repeated = client.post(path + '/apply', headers=auth(admin_token), json=body)
    assert repeated.status_code == 200 and repeated.json()['idempotent']
    changed = client.post(path + '/apply', headers=auth(admin_token),
                          json={**body, 'expected_proposal_hash': '0' * 64})
    assert changed.status_code == 409, changed.text


@pytest.mark.parametrize('concurrency', [2, 8, 32])
def test_concurrent_apply_is_one_revision_postgresql(client, admin_token, proposal_case, db, concurrency):
    if db.bind.dialect.name != 'postgresql':
        pytest.skip('Row-lock concurrency proof requires PostgreSQL')
    path, proposal, job_id = proposal_case
    barrier = Barrier(concurrency, timeout=10)
    headers, body = auth(admin_token), _body(proposal)
    def submit(_):
        barrier.wait()
        return client.post(path + '/apply', headers=headers, json=body)
    # Each HTTP request has its own application DB session. This is a real
    # row-lock interleaving, not a sequential mock returning an accepted flag.
    started = monotonic()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        responses = list(pool.map(submit, range(concurrency)))
    # This is a generous regression bound, not a production latency SLO.
    # The original circular pool wait hit the default 30-second timeout.
    assert monotonic() - started < 20
    assert all(r.status_code == 200 for r in responses), [(r.status_code, r.text) for r in responses]
    assert sum(r.json().get('applied') is True for r in responses) == 1
    db.expire_all()
    doc = db.query(EditorDocument).filter_by(job_id=job_id).one()
    assert doc.revision == 1 and doc.current_segments[0]['text'] == 'Hola, mundo!'


def test_analyze_apply_contention_keeps_one_saved_revision(client, admin_token, proposal_case, db):
    path, proposal, job_id = proposal_case
    barrier = Barrier(8, timeout=10)
    headers, body = auth(admin_token), _body(proposal)
    def submit(index):
        barrier.wait()
        if index % 2:
            return client.post(path.rsplit('/', 1)[0], headers=headers)
        return client.post(path + '/apply', headers=headers, json=body)
    started = monotonic()
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(submit, range(8)))
    assert monotonic() - started < 20
    assert all(r.status_code == 200 for r in responses), [(r.status_code, r.text) for r in responses]
    db.expire_all()
    doc = db.query(EditorDocument).filter_by(job_id=job_id).one()
    assert doc.revision == 1 and doc.current_segments[0]['text'] == 'Hola, mundo!'
