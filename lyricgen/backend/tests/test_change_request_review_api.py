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
    db.flush()
    db.refresh(job)
    from tests.test_deliveries import _signed_umg_qc_report
    job.delivery_qc = _signed_umg_qc_report(job, job.approved_by)
    db.commit()
    document = get_or_create_document(db, job.job_id, job.tenant_id, job.segments_json)
    db.commit()
    published = client.post(f'/admin/deliveries/from-job/{job.job_id}', headers=auth(admin_token), json={})
    assert published.status_code == 200, published.text
    cr = DeliveryChangeRequest(delivery_id=published.json()['delivery_id'], comment='Corregir la letra completa',
                               submitted_at=datetime.now(timezone.utc) - timedelta(minutes=2))
    db.add(cr); db.commit()
    return job, document, cr


def test_change_request_list_can_reconcile_one_exact_request(client, admin_token, manual_request, db):
    _, _, request = manual_request
    sibling = DeliveryChangeRequest(delivery_id=request.delivery_id, comment='Otro pedido')
    db.add(sibling)
    db.commit()

    response = client.get(
        f'/admin/change-requests?status=all&change_request_id={request.id}&limit=1',
        headers=auth(admin_token),
    )

    assert response.status_code == 200, response.text
    assert [item['id'] for item in response.json()['items']] == [request.id]


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
    from tests.test_deliveries import _signed_umg_qc_report
    db.flush()
    db.refresh(job)
    job.delivery_qc = _signed_umg_qc_report(job, job.approved_by)
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


def _published_without_ticking(client, admin_token, manual_request, db):
    """A send published the corrected cut but left the request open."""
    job, document, cr = manual_request
    now = datetime.now(timezone.utc)
    document.updated_at = now
    job.render_params = {'_rendered_segments_revision': document.revision, '_rendered_at': now.isoformat()}
    db.commit()
    delivery = db.get(Delivery, cr.delivery_id)
    assert delivery.published_render_fingerprint == render_fingerprint(job)
    delivery.content_updated_at = now
    db.commit()
    return job, document, cr, delivery


def test_cut_published_without_ticking_closes_as_publication_in_one_click(client, admin_token, manual_request, db):
    job, document, cr, delivery = _published_without_ticking(client, admin_token, manual_request, db)
    listed = client.get(f'/admin/change-requests?status=all&change_request_id={cr.id}&limit=1',
                        headers=auth(admin_token)).json()['items'][0]
    assert listed['publication']['answers_request'] is True
    assert 'confirm_publication' in listed['workflow']['allowed_actions']

    stale = client.post(f'/admin/change-requests/{cr.id}/confirm-publication', headers=auth(admin_token),
                        json={'reviewed_render_fingerprint': 'another-cut',
                              'reviewed_editor_revision': document.revision})
    assert stale.status_code == 409, stale.text

    body = {'reviewed_render_fingerprint': render_fingerprint(job),
            'reviewed_editor_revision': document.revision, 'confirmed_items': 2}
    response = client.post(f'/admin/change-requests/{cr.id}/confirm-publication',
                           headers=auth(admin_token), json=body)
    assert response.status_code == 200, response.text
    db.expire_all()
    closed = db.get(DeliveryChangeRequest, cr.id)
    assert closed.resolution_source == 'publication'
    assert closed.resolved_by_revision == delivery.published_revision
    assert closed.resolution_note == f'Resuelto al publicar la versión {delivery.published_revision}.'
    from database import AuditLog
    audit = (db.query(AuditLog).filter(AuditLog.action == 'delivery.change_request.resolve')
             .order_by(AuditLog.id.desc()).first())
    assert audit.detail['source'] == 'publication_confirmed' and audit.detail['confirmed_items'] == 2
    assert listed['workflow']['label'] == 'Publicado · revisá cada punto del pedido'
    again = client.post(f'/admin/change-requests/{cr.id}/confirm-publication',
                        headers=auth(admin_token), json=body)
    assert again.json()['already_resolved'] is True


def test_publication_close_refused_when_no_fix_was_saved_after_request(client, admin_token, manual_request, db):
    job, document, cr, delivery = _published_without_ticking(client, admin_token, manual_request, db)
    document.updated_at = cr.submitted_at - timedelta(minutes=5)
    db.commit()
    response = client.post(f'/admin/change-requests/{cr.id}/confirm-publication', headers=auth(admin_token),
                           json={'reviewed_render_fingerprint': render_fingerprint(job),
                                 'reviewed_editor_revision': document.revision})
    assert response.status_code == 409, response.text
    assert response.json()['detail']['reason'] == 'no_fix_after_request'
    db.expire_all()
    assert db.get(DeliveryChangeRequest, cr.id).resolved_at is None


def _published_text_mismatch(client, admin_token, manual_request, db, monkeypatch):
    from database import EditorVersion
    monkeypatch.setenv('CHANGE_REQUEST_PUBLISH_GUARD_ENABLED', '1')
    job, document, cr, delivery = _published_without_ticking(client, admin_token, manual_request, db)
    cr.comment = '0:00 Letra completa coregida a mano'
    version = db.query(EditorVersion).filter_by(job_id=job.job_id, revision=document.revision).one()
    version.segments = document.current_segments
    version.is_approved = True
    db.commit()
    body = {'reviewed_render_fingerprint': render_fingerprint(job),
            'reviewed_editor_revision': document.revision, 'confirmed_items': 1}
    url = f'/admin/change-requests/{cr.id}/confirm-publication'
    first = client.post(url, headers=auth(admin_token), json=body)
    assert first.status_code == 409, first.text
    detail = first.json()['detail']
    assert detail['code'] == 'requested_text_not_found'
    assert detail['missing'] == ['Letra completa coregida a mano']
    assert len(detail['review_token']) == 64
    db.expire_all()
    assert cr.resolved_at is None
    return job, document, cr, delivery, body, url, detail['review_token']


def test_manual_text_review_closes_exact_published_cut_with_reason_and_audit(
        client, admin_token, manual_request, db, monkeypatch):
    from database import AuditLog
    job, document, cr, delivery, body, url, token = _published_text_mismatch(
        client, admin_token, manual_request, db, monkeypatch)
    before = (delivery.published_revision, dict(delivery.published_file_keys), render_fingerprint(job),
              list(document.current_segments), cr.comment)
    other = DeliveryChangeRequest(delivery_id=delivery.id, comment='Otro cambio')
    db.add(other); db.commit()
    reason = 'El pedido tiene un typo; verifiqué corregida en el video publicado.'
    response = client.post(url, headers=auth(admin_token), json={**body,
                           'text_review_token': token, 'text_review_reason': reason})
    assert response.status_code == 200, response.text
    db.expire_all()
    assert cr.resolved_by_revision == delivery.published_revision
    assert cr.resolution_source == 'publication'
    assert cr.resolution_note == f'Revisión manual de la versión {delivery.published_revision}: {reason}'
    assert other.resolved_at is None
    assert before == (delivery.published_revision, delivery.published_file_keys, render_fingerprint(job),
                      document.current_segments, cr.comment)
    audit = db.query(AuditLog).filter_by(action='delivery.change_request.resolve').order_by(AuditLog.id.desc()).first()
    assert audit.detail['manual_text_review'] == {
        'reason': reason, 'missing': ['Letra completa coregida a mano'], 'review_token': token}
    assert client.post(url, headers=auth(admin_token), json=body).json()['already_resolved'] is True


@pytest.mark.parametrize('missing', ['reason', 'checklist', 'token'])
def test_manual_text_review_requires_reason_checklist_and_server_discrepancy_token(
        client, admin_token, manual_request, db, monkeypatch, missing):
    _, _, cr, _, body, url, token = _published_text_mismatch(client, admin_token, manual_request, db, monkeypatch)
    body.update(text_review_token=token, text_review_reason='Verifiqué el pedido contra el video.')
    if missing == 'reason':
        body['text_review_reason'] = '   '
    elif missing == 'checklist':
        del body['confirmed_items']
    else:
        del body['text_review_token']
    response = client.post(url, headers=auth(admin_token), json=body)
    assert response.status_code == (409 if missing == 'token' else 422), response.text
    db.expire_all()
    assert cr.resolved_at is None


@pytest.mark.parametrize('change', ['request', 'corrected_request', 'editor', 'cut', 'guard_disabled'])
def test_manual_text_review_cannot_reuse_evidence_after_request_editor_or_cut_changes(
        client, admin_token, manual_request, db, monkeypatch, change):
    job, document, cr, _, body, url, token = _published_text_mismatch(client, admin_token, manual_request, db, monkeypatch)
    if change == 'request':
        cr.comment = '0:00 Una corrección nueva del cliente'
    elif change == 'corrected_request':
        cr.comment = '0:00 Letra completa corregida a mano'
    elif change == 'guard_disabled':
        monkeypatch.setenv('CHANGE_REQUEST_PUBLISH_GUARD_ENABLED', '0')
    elif change == 'editor':
        document.revision += 1
    else:
        job.edit_count = (job.edit_count or 0) + 1
    db.commit()
    response = client.post(url, headers=auth(admin_token), json={**body,
                           'text_review_token': token, 'text_review_reason': 'Verificado antes del cambio.'})
    assert response.status_code == 409, response.text
    db.expire_all()
    assert cr.resolved_at is None


def test_manual_text_review_keeps_admin_permission(client, admin_token, user_token, manual_request, db, monkeypatch):
    _, _, cr, _, body, url, token = _published_text_mismatch(client, admin_token, manual_request, db, monkeypatch)
    response = client.post(url, headers=auth(user_token), json={**body,
                           'text_review_token': token, 'text_review_reason': 'Verificado.'})
    assert response.status_code == 403
    db.expire_all()
    assert cr.resolved_at is None
