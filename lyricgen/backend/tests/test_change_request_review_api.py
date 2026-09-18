from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from tests.conftest import auth
from tests.test_deliveries import approved_job, all_r2_files_present, _publication_copy_succeeds, _portal_token_env, PORTAL_TOKEN
from database import Delivery, DeliveryChangeRequest, JobOutboxEvent
from editor import get_or_create_document
from delivery_freshness import render_fingerprint


@pytest.fixture
def manual_request(client, admin_token, approved_job, db, all_r2_files_present):
    job = approved_job
    job.segments_json = [{'start': 0, 'end': 3, 'text': 'Letra completa corregida a mano'}]
    job.bg_r2_key_cached = 'backgrounds/test.mp4'
    db.commit()
    document = get_or_create_document(db, job.job_id, job.tenant_id, job.segments_json)
    db.commit()
    published = client.post(f'/admin/deliveries/from-job/{job.job_id}', headers=auth(admin_token), json={})
    assert published.status_code == 200, published.text
    cr = DeliveryChangeRequest(delivery_id=published.json()['delivery_id'], comment='Corregir la letra completa',
                               submitted_at=datetime.now(timezone.utc) - timedelta(minutes=2))
    db.add(cr); db.commit()
    return job, document, cr


def test_manual_review_render_uses_exact_saved_revision_and_deduplicates(client, admin_token, manual_request, db):
    job, document, cr = manual_request
    review = client.get(f'/admin/change-requests/{cr.id}/review', headers=auth(admin_token)).json()
    assert review['segments'][0]['text'] == 'Letra completa corregida a mano'
    assert review['editor_revision'] == document.revision
    stale = client.post(f'/admin/change-requests/{cr.id}/render', headers=auth(admin_token),
                        json={'editor_revision': document.revision + 1})
    assert stale.status_code == 409, stale.text
    with patch('main.enqueue_edit', return_value='test-edit'):
        for _ in range(2):
            response = client.post(f'/admin/change-requests/{cr.id}/render', headers=auth(admin_token),
                                   json={'editor_revision': document.revision})
            assert response.status_code == 202, response.text
    db.expire_all()
    events = db.query(JobOutboxEvent).filter(JobOutboxEvent.job_id == job.job_id,
                                          JobOutboxEvent.event_type == 'edit.enqueue').all()
    assert len(events) == 1
    assert events[0].payload['edit_params']['segments'][0]['text'] == review['segments'][0]['text']
    assert events[0].payload['edit_params']['_confirmed_segments_revision'] == document.revision
    assert cr.resolved_at is None
    job.status = 'error'
    job.error = 'Render worker failed'
    db.commit()
    with patch('main.enqueue_edit', return_value='test-retry'):
        retried = client.post(f'/admin/change-requests/{cr.id}/render', headers=auth(admin_token),
                              json={'editor_revision': document.revision})
    # A failed worker must not replay the old 202 or claim to be rendering.
    # Recovery uses the existing failed-job retry flow, not a duplicate edit.
    assert retried.status_code == 400, retried.text
    db.expire_all()
    assert db.query(JobOutboxEvent).filter(JobOutboxEvent.job_id == job.job_id,
                                         JobOutboxEvent.event_type == 'edit.enqueue').count() == 1


def test_publish_confirms_finished_cut_and_resolves_only_selected_request(client, admin_token, manual_request, db, all_r2_files_present):
    job, document, cr = manual_request
    other = DeliveryChangeRequest(delivery_id=cr.delivery_id, comment='Otro pedido sin revisar')
    db.add(other)
    job.status = 'pending_review'
    job.previous_versions = [{'archived_at': datetime.now(timezone.utc).isoformat()}]
    job.render_params = {'_rendered_segments_revision': document.revision,
                         '_rendered_at': datetime.now(timezone.utc).isoformat()}
    db.commit()
    body = dict(portal_id='argentina', change_request_id=cr.id,
                reviewed_render_fingerprint=render_fingerprint(job), reviewed_editor_revision=document.revision)
    wrong = client.post(f'/admin/deliveries/from-job/{job.job_id}', headers=auth(admin_token),
                        json={**body, 'reviewed_render_fingerprint': 'wrong'})
    assert wrong.status_code == 409
    response = client.post(f'/admin/deliveries/from-job/{job.job_id}', headers=auth(admin_token), json=body)
    assert response.status_code == 200, response.text
    assert response.json()['revision'] == 2
    assert response.json()['resolved_change_requests'] == [cr.id]
    db.expire_all()
    assert cr.resolved_at is not None
    assert other.resolved_at is None
    assert job.status == 'done'
    assert db.get(Delivery, cr.delivery_id).published_file_keys


@pytest.mark.parametrize('portal', ['argentina', 'chile'])
def test_manual_resolution_is_visible_in_portal_without_publishing(client, admin_token, manual_request, db, portal):
    job, _, cr = manual_request
    before = db.get(Delivery, cr.delivery_id).published_file_keys
    db.get(Delivery, cr.delivery_id).portal_id = portal
    db.commit()
    response = client.post(f'/admin/change-requests/{cr.id}/resolve', headers=auth(admin_token),
                           json={'resolution_note': 'Verificado manualmente'})
    assert response.status_code == 200
    listing = client.get('/api/deliveries/items', headers={'X-Portal-Token': PORTAL_TOKEN, 'X-Portal-Id': portal}).json()
    version = next(v for song in listing['songs'] for v in song['versions'] if v['job_id'] == job.job_id)
    request = next(row for row in version['change_requests'] if row['id'] == cr.id)
    assert request['resolved_at']
    assert request['resolution_source'] == 'manual'
    assert version['pending_change_requests'] == 0
    db.expire_all()
    assert db.get(Delivery, cr.delivery_id).published_file_keys == before


def test_failed_snapshot_does_not_change_portal_or_resolve_request(client, admin_token, manual_request, db, all_r2_files_present):
    job, _, cr = manual_request
    before = dict(db.get(Delivery, cr.delivery_id).published_file_keys)
    job.previous_versions = [{'archived_at': datetime.now(timezone.utc).isoformat()}]
    db.commit()
    with patch('storage.copy_object', return_value=False):
        response = client.post(f'/admin/deliveries/from-job/{job.job_id}', headers=auth(admin_token), json={})
    assert response.status_code == 503
    db.expire_all()
    assert db.get(Delivery, cr.delivery_id).published_file_keys == before
    assert cr.resolved_at is None


@pytest.mark.parametrize('status', ['done', 'rejected'])
@pytest.mark.parametrize('edit_type', ['background', 'custom'])
def test_admin_can_change_approved_background_without_publishing(client, admin_token, manual_request, db, status, edit_type):
    job, _, cr = manual_request
    delivery = db.get(Delivery, cr.delivery_id)
    published = dict(delivery.published_file_keys)
    job.status = status
    db.commit()
    body = {'edit_type': edit_type, 'background_hint': 'Paisaje nocturno sin personas',
            'force_content_validation': True}
    if edit_type == 'custom':
        from tests.test_edit_custom_background import _valid_key
        body['custom_background_r2_key'] = _valid_key(job.tenant_id, job.job_id)
    with patch('main.enqueue_edit', return_value='test-background'):
        response = client.post(f'/edit/{job.job_id}', headers=auth(admin_token), json=body)
    assert response.status_code == 202, response.text
    db.expire_all()
    assert job.status == 'editing'
    assert delivery.published_file_keys == published
    assert cr.resolved_at is None


def test_approved_multiscene_background_still_requires_scene_editor(client, admin_token, manual_request, db):
    job, _, _ = manual_request
    job.scene_plan = {'scenes': [{'index': 0}]}
    db.commit()
    response = client.post(f'/edit/{job.job_id}', headers=auth(admin_token),
                           json={'edit_type': 'background', 'background_hint': 'Otro paisaje'})
    assert response.status_code == 400
    assert 'multi-escena' in response.text


def test_approved_background_permission_is_not_granted_to_regular_users(client, user_token, manual_request, db):
    job, _, _ = manual_request
    identity = client.get('/auth/me', headers=auth(user_token)).json()
    job.user_id = identity['id']
    job.tenant_id = identity['tenant_id']
    db.commit()
    with patch('main.enqueue_edit') as enqueue:
        response = client.post(f'/edit/{job.job_id}', headers=auth(user_token),
                               json={'edit_type': 'background', 'background_hint': 'Otro paisaje'})
    assert response.status_code == 400, response.text
    enqueue.assert_not_called()
    db.expire_all()
    assert job.status == 'done'
