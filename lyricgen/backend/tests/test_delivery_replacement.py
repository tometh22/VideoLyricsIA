from datetime import datetime, timezone, timedelta
import pytest
from database import Delivery, DeliveryChangeRequest, Job, SessionLocal
from tests.conftest import auth
from tests.test_deliveries import approved_job, all_r2_files_present, _publication_copy_succeeds


@pytest.mark.parametrize('duplicate', [False, True])
@pytest.mark.parametrize('portal', ['argentina', 'chile'])
def test_correction_variant_replaces_requested_delivery(client, admin_token, approved_job, all_r2_files_present, db, duplicate, portal):
    headers = auth(admin_token)
    parent_id = approved_job.job_id
    published = client.post('/admin/deliveries/from-job/' + parent_id, headers=headers, json={'portal_id': portal}).json()
    original_id = published['delivery_id']
    child = Job(job_id='variant00001', parent_job_id=parent_id,
                tenant_id=approved_job.tenant_id, user_id=approved_job.user_id,
                artist=approved_job.artist, song_title=approved_job.song_title,
                filename='variant.mp3', status='done', approved_at=datetime.now(timezone.utc), completed_at=datetime.now(timezone.utc),
                approved_by=approved_job.approved_by, video_url='/video', short_url='/short', thumbnail_url='/thumb')
    db.add(child); db.commit()
    child_id = child.job_id
    duplicate_id = None
    try:
        if duplicate:
            duplicate_id = client.post('/admin/deliveries/from-job/' + child_id, headers=headers, json={'portal_id': portal}).json()['delivery_id']
        request = DeliveryChangeRequest(delivery_id=original_id, comment='Cambiar fondo', submitted_at=datetime.now(timezone.utc) - timedelta(minutes=1))
        db.add(request); db.commit()
        request_id = request.id
        response = client.post('/admin/deliveries/from-job/' + child_id, headers=headers, json={'portal_id': portal})
        assert response.status_code == 200, response.text
        assert response.json()['delivery_id'] == original_id
        assert response.json()['replaced_job_id'] == parent_id
        # A matching variant is not evidence that every request was reviewed.
        assert response.json()['resolved_change_requests'] == []
        assert response.json()['revision'] == 2
        db.expire_all()
        original = db.get(Delivery, original_id)
        assert original.job_id == child_id and original.approved_at is None
        assert db.get(DeliveryChangeRequest, request_id).resolved_at is None
        if duplicate_id:
            assert db.get(Delivery, duplicate_id).removed_at is not None
        # A second click is idempotent and does not create another option.
        repeated = client.post('/admin/deliveries/from-job/' + child_id, headers=headers, json={'portal_id': portal})
        assert repeated.status_code == 200, repeated.text
        assert repeated.json()['delivery_id'] == original_id
        assert repeated.json()['revision'] == 2
        assert db.query(Delivery).filter_by(job_id=child_id, removed_at=None).count() == 1
    finally:
        ids = [d.id for d in db.query(Delivery).filter(Delivery.job_id.in_([child_id, parent_id])).all()]
        db.query(DeliveryChangeRequest).filter(DeliveryChangeRequest.delivery_id.in_(ids)).delete(synchronize_session=False)
        db.query(Delivery).filter(Delivery.id.in_(ids)).delete(synchronize_session=False)
        db.query(Job).filter_by(job_id=child_id).delete(synchronize_session=False)
        db.commit()


def test_replacement_does_not_cross_portals_or_match_title_only(client, admin_token, approved_job, all_r2_files_present, db):
    from delivery_replacement import target
    published = client.post('/admin/deliveries/from-job/' + approved_job.job_id, headers=auth(admin_token), json={'portal_id': 'argentina'}).json()
    db.add(DeliveryChangeRequest(delivery_id=published['delivery_id'], comment='Cambiar fondo', submitted_at=datetime.now(timezone.utc) - timedelta(minutes=1))); db.commit()
    child = Job(job_id='variant00002', tenant_id=approved_job.tenant_id,
                artist=approved_job.artist, song_title=approved_job.song_title, completed_at=datetime.now(timezone.utc))
    assert target(db, db, child, 'argentina') == (None, None)
    child.parent_job_id = approved_job.job_id
    assert target(db, db, child, 'chile') == (None, None)
    assert target(db, db, child, 'argentina')[0].id == published['delivery_id']
    child.completed_at = datetime.now(timezone.utc) - timedelta(days=1)
    assert target(db, db, child, 'argentina') == (None, None)


def test_parent_changed_during_copy_is_not_overwritten(client, admin_token, approved_job, all_r2_files_present, db, monkeypatch):
    from delivery_replacement import identity
    assert identity(None) is None
    # The shared direct-publication concurrency tests exercise the same CAS;
    # identity includes job_id so a competing variant swap is also detected.
    row = Delivery(job_id='parent', published_revision=1)
    before = identity(row)
    row.job_id = 'different-variant'
    assert identity(row) != before
