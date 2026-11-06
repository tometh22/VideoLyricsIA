"""Regression: >60s media copies must not retain transactions or stale authority."""
from contextlib import contextmanager

import pytest

from database import Delivery, Job, SessionLocal, get_db, get_deliveries_db
from tests.conftest import auth
from tests.test_deliveries import approved_job, all_r2_files_present, _publication_copy_succeeds


@pytest.fixture
def tracked_sessions(client):
    from main import app
    sessions = []
    def tracked():
        with SessionLocal() as session:
            sessions.append(session)
            yield session
    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = tracked
    app.dependency_overrides[get_deliveries_db] = tracked
    yield sessions
    app.dependency_overrides.clear()
    app.dependency_overrides.update(previous)


def test_large_publication_copy_has_no_transaction_or_event_loop(
        client, admin_token, approved_job, all_r2_files_present, tracked_sessions, monkeypatch):
    import asyncio
    calls = []
    def copy(tenant, jid, types):
        assert tracked_sessions and all(not s.in_transaction() for s in tracked_sessions)
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
        calls.append(jid)
        return {kind: 'frozen/' + kind for kind in types}
    monkeypatch.setattr('delivery_snapshots.copy_snapshot', copy)
    response = client.post('/admin/deliveries/from-job/' + approved_job.job_id,
                           headers=auth(admin_token), json={})
    assert response.status_code == 200, response.text
    assert calls == [approved_job.job_id]


def test_edit_during_copy_cannot_publish_stale_cut(
        client, admin_token, approved_job, all_r2_files_present, tracked_sessions, monkeypatch, db):
    jid = approved_job.job_id
    def copy(tenant, job_id, types):
        assert all(not s.in_transaction() for s in tracked_sessions)
        with SessionLocal() as other:
            job = other.query(Job).filter_by(job_id=job_id).one()
            job.segments_revision = int(job.segments_revision or 0) + 1
            other.commit()
        return {kind: 'frozen/' + kind for kind in types}
    monkeypatch.setattr('delivery_snapshots.copy_snapshot', copy)
    response = client.post('/admin/deliveries/from-job/' + jid,
                           headers=auth(admin_token), json={})
    assert response.status_code == 409, response.text
    assert db.query(Delivery).filter_by(job_id=jid).count() == 0


def test_competing_publication_is_not_overwritten(
        client, admin_token, approved_job, all_r2_files_present, monkeypatch, db):
    jid = approved_job.job_id
    first = client.post('/admin/deliveries/from-job/' + jid, headers=auth(admin_token), json={})
    delivery = db.get(Delivery, first.json()['delivery_id'])
    delivery.published_file_keys = None
    db.commit()
    delivery_id = delivery.id
    def copy(tenant, job_id, types):
        with SessionLocal() as other:
            newer = other.get(Delivery, delivery_id)
            newer.published_revision = 77
            newer.published_file_keys = {'video': 'newer-human-cut'}
            other.commit()
        return {kind: 'stale-copy/' + kind for kind in types}
    monkeypatch.setattr('delivery_snapshots.copy_snapshot', copy)
    response = client.post('/admin/deliveries/from-job/' + jid, headers=auth(admin_token), json={})
    assert response.status_code == 409, response.text
    db.expire_all()
    assert db.get(Delivery, delivery_id).published_file_keys == {'video': 'newer-human-cut'}


@pytest.mark.parametrize('competing_pin', [False, True])
def test_legacy_pin_releases_transaction_and_preserves_concurrent_publisher(
        client, admin_token, approved_job, all_r2_files_present, db, monkeypatch, competing_pin):
    from delivery_snapshots import pin_legacy_deliveries
    jid = approved_job.job_id
    response = client.post('/admin/deliveries/from-job/' + jid, headers=auth(admin_token), json={})
    delivery_id = response.json()['delivery_id']
    row = db.get(Delivery, delivery_id)
    row.published_file_keys = None
    db.commit()
    sessions = []
    @contextmanager
    def scoped():
        with SessionLocal() as session:
            sessions.append(session)
            yield session
    def copy(tenant, job_id, types, **kwargs):
        assert all(not s.in_transaction() for s in sessions)
        if competing_pin:
            with SessionLocal() as other:
                other.get(Delivery, delivery_id).published_file_keys = {'video': 'winner'}
                other.commit()
        return {'video': 'preserved-old-cut'}
    monkeypatch.setattr('database.scoped_deliveries_db', scoped)
    monkeypatch.setattr('delivery_snapshots.copy_snapshot', copy)
    pin_legacy_deliveries(jid)
    db.expire_all()
    assert db.get(Delivery, delivery_id).published_file_keys == {
        'video': 'winner' if competing_pin else 'preserved-old-cut'}
