"""What the client's portal shows while a delivery has unpublished changes."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import delivery_snapshots as ds
import system_settings
from database import AuditLog, Delivery, SessionLocal
from tests.conftest import auth
from tests.test_edit_metadata import _admin_identity, _create_pending_review_job

NOW = datetime.now(timezone.utc)


def row(**kw):
    base = dict(tenant_snapshot='t', job_id='j', published_file_keys=None,
                stale_since=None, stale_reason=None, client_visibility=None)
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _fresh_settings():
    system_settings.clear_cache()
    yield
    system_settings.clear_cache()


def test_auto_hides_everything_while_changes_are_unpublished_and_shows_after_publish():
    editing = row(stale_since=NOW, stale_reason='editing')
    assert ds.is_hidden_from_client(editing) is True
    assert ds.portal_key(editing, 'video', for_client=True) is None
    assert ds.portal_key(editing, 'thumbnail', for_client=True) is None
    published = row()
    assert ds.is_hidden_from_client(published) is False
    assert ds.portal_key(published, 'video', for_client=True).endswith('lyric_video.mp4')


def test_only_in_flight_changes_auto_hide_a_dead_edit_or_unknown_reason_does_not():
    # A failed edit has nothing to "publish away": hiding it would strand the client
    # with no way out for the operator, and it was served before this rule existed.
    assert ds.is_hidden_from_client(row(stale_since=NOW, stale_reason='edit_failed')) is False
    assert ds.is_hidden_from_client(row(stale_since=NOW, stale_reason=None)) is False
    assert ds.is_hidden_from_client(row(stale_since=NOW, stale_reason='prores_pending')) is True
    assert ds.is_hidden_from_client(row(stale_since=NOW, stale_reason='edit_failed', client_visibility='hidden')) is True


def test_a_frozen_snapshot_is_never_auto_hidden():
    frozen = row(stale_since=NOW, stale_reason='editing', published_file_keys={'video': 'k.published-1'})
    assert ds.is_hidden_from_client(frozen) is False
    assert ds.portal_key(frozen, 'video', for_client=True) == 'k.published-1'


def test_manual_hidden_wins_and_manual_visible_forces_the_mp4_but_never_a_stale_master():
    assert ds.is_hidden_from_client(row(client_visibility='hidden')) is True
    assert ds.portal_key(row(client_visibility='hidden', published_file_keys={'video': 'k'}), 'video', for_client=True) is None
    forced = row(client_visibility='visible', stale_since=NOW, stale_reason='editing')
    assert ds.is_hidden_from_client(forced) is False
    assert ds.portal_key(forced, 'video', for_client=True).endswith('lyric_video.mp4')
    assert ds.portal_key(forced, 'umg_master', for_client=True) is None


def test_the_admin_preview_still_sees_what_is_published_while_the_client_does_not():
    editing = row(stale_since=NOW, stale_reason='editing')
    assert ds.portal_key(editing, 'video').endswith('lyric_video.mp4')


def test_unknown_values_behave_like_auto():
    assert ds.client_visibility_mode(row(client_visibility='nonsense')) == 'auto'


def _make(db, job_id, tenant_id, user_id, **kw):
    delivery = Delivery(job_id=job_id, label='Campaña', file_types=['video'], artist_snapshot='A',
                        song_title_snapshot='S', tenant_snapshot=tenant_id, added_by_user_id=user_id,
                        portal_id='argentina', published_revision=1, **kw)
    db.add(delivery); db.commit()
    return delivery.id


def _cleanup(ids):
    with SessionLocal() as db:
        db.query(AuditLog).filter(AuditLog.action == 'delivery.visibility').delete(synchronize_session=False)
        db.query(Delivery).filter(Delivery.id.in_(ids)).delete(synchronize_session=False)
        db.commit()


def test_admin_sets_the_mode_audits_it_and_rejects_garbage(client, admin_token, db):
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_pending_review_job(db, tenant_id, user_id, s3_keys={'video': 't/j/lyric_video.mp4'})
    delivery_id = _make(db, job_id, tenant_id, user_id)
    _cleanup([])   # ids are reused after deletes: start from a clean audit trail
    try:
        url = f'/admin/deliveries/{delivery_id}/visibility'
        bad = client.put(url, headers=auth(admin_token), json={'mode': 'maybe'})
        assert bad.status_code == 422
        ok = client.put(url, headers=auth(admin_token), json={'mode': 'hidden'})
        assert ok.status_code == 200 and ok.json() == {'ok': True, 'client_visibility': 'hidden', 'hidden_from_client': True}
        with SessionLocal() as other:
            assert other.get(Delivery, delivery_id).client_visibility == 'hidden'
            audit = [a for a in other.query(AuditLog).filter(AuditLog.action == 'delivery.visibility').all()
                     if a.detail['delivery_id'] == delivery_id]
            assert [a.detail['to'] for a in audit] == ['hidden'] and audit[0].detail['from'] == 'auto'
        back = client.put(url, headers=auth(admin_token), json={'mode': 'auto'})
        assert back.json()['hidden_from_client'] is False
        with SessionLocal() as other:
            assert other.get(Delivery, delivery_id).client_visibility is None
        assert client.put('/admin/deliveries/99999999/visibility', headers=auth(admin_token), json={'mode': 'auto'}).status_code == 404
    finally:
        _cleanup([delivery_id])


def test_portal_listing_keeps_the_card_but_drops_the_files_while_hidden(client, admin_token, db, monkeypatch):
    import storage
    import queue_jobs
    monkeypatch.setenv('DELIVERY_PORTAL_TOKEN_ARGENTINA', 'synthetic-visibility-token')
    monkeypatch.setattr(queue_jobs, '_init_redis', lambda: (None, None, None))
    monkeypatch.setattr(storage, '_get_metadata_client', lambda: SimpleNamespace(head_object=lambda **kw: {'ContentLength': 10}))
    monkeypatch.setattr(storage, 'generate_signed_url', lambda key, **kw: 'https://synthetic.invalid/' + key)
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_pending_review_job(db, tenant_id, user_id, s3_keys={'video': 't/j/lyric_video.mp4'})
    delivery_id = _make(db, job_id, tenant_id, user_id)
    headers = {'X-Portal-Token': 'synthetic-visibility-token', 'X-Portal-Id': 'argentina'}

    def version():
        body = client.get('/api/deliveries/items', headers=headers)
        assert body.status_code == 200, body.text
        return [v for s in body.json()['songs'] for v in s['versions'] if v['delivery_id'] == delivery_id][0]

    try:
        shown = version()
        assert shown['files_hidden'] is False
        assert [f['type'] for f in shown['files']] == ['video', 'umg_master']
        assert shown['files'][1]['can_prepare'] is True
        with SessionLocal() as other:
            other.get(Delivery, delivery_id).stale_since = NOW
            other.get(Delivery, delivery_id).stale_reason = 'editing'
            other.commit()
        auto = version()
        assert auto['files_hidden'] is True and auto['files'] == [] and auto['preview_url'] is None
        assert auto['updating'] is True
        client.put(f'/admin/deliveries/{delivery_id}/visibility', headers=auth(admin_token), json={'mode': 'visible'})
        assert [f['type'] for f in version()['files']] == ['video', 'umg_master']
        client.put(f'/admin/deliveries/{delivery_id}/visibility', headers=auth(admin_token), json={'mode': 'hidden'})
        assert version()['files'] == []
    finally:
        _cleanup([delivery_id])


def test_publication_settings_switch_overrides_the_env_default_and_is_audited(client, admin_token, db, monkeypatch):
    monkeypatch.delenv('PUBLISH_LATEST_POINTER', raising=False)
    try:
        assert client.get('/admin/publication-settings', headers=auth(admin_token)).json() == {
            'publication_mode': 'snapshot', 'source': 'default'}
        on = client.put('/admin/publication-settings', headers=auth(admin_token), json={'publication_mode': 'pointer'})
        assert on.json() == {'publication_mode': 'pointer', 'source': 'panel'}
        assert ds.latest_pointer_enabled() is True
        # The panel beats the env var in both directions.
        monkeypatch.setenv('PUBLISH_LATEST_POINTER', '1')
        off = client.put('/admin/publication-settings', headers=auth(admin_token), json={'publication_mode': 'snapshot'})
        assert off.json()['publication_mode'] == 'snapshot' and ds.latest_pointer_enabled() is False
        assert client.put('/admin/publication-settings', headers=auth(admin_token),
                          json={'publication_mode': 'x'}).status_code == 422
        actions = [(a.detail['from'], a.detail['to']) for a in db.query(AuditLog).filter(AuditLog.action == 'publication.mode').order_by(AuditLog.id)]
        assert actions[-2:] == [('snapshot', 'pointer'), ('pointer', 'snapshot')]
    finally:
        from database import SystemSetting
        with SessionLocal() as other:
            other.query(SystemSetting).delete(); 
            other.query(AuditLog).filter(AuditLog.action == 'publication.mode').delete(synchronize_session=False)
            other.commit()
        system_settings.clear_cache()
