import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from admin import router
from auth import get_current_user
from batch_campaigns import _campaign_or_404
from database import AuditLog, BatchCampaign, BatchCampaignReviewer, User, get_db
from fastapi import HTTPException


@pytest.fixture
def access_env(db, monkeypatch):
    prefix = 'access-' + uuid.uuid4().hex[:8]
    monkeypatch.setenv('BATCH_CAMPAIGN_ENABLED', '1')
    monkeypatch.setenv('BATCH_CAMPAIGN_SCOPES', prefix)
    admin = User(username=prefix+'-admin', tenant_id=prefix, role='admin', hashed_password='unused', is_active=True)
    target = User(username=prefix+'-reviewer', tenant_id=prefix, role='user', hashed_password='unused', is_active=True)
    db.add_all([admin, target]); db.flush()
    campaigns = [BatchCampaign(id=uuid.uuid4().hex[:12], name=prefix+name, tenant_id=tenant,
                    created_by=admin.id, kind='lyric_video', status='active')
                 for name, tenant in [(' external', prefix+'-other'), (' own', prefix), (' 100%', prefix+'-other')]]
    db.add_all(campaigns); db.commit()
    actor = {'id': admin.id, 'role': 'admin', 'tenant_id': prefix}
    app = FastAPI(); app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: actor
    ids = [c.id for c in campaigns]; user_ids = [admin.id, target.id]
    try:
        with TestClient(app) as client:
            yield client, db, actor, target, campaigns
    finally:
        db.rollback()
        db.query(AuditLog).filter(AuditLog.user_id.in_(user_ids)).delete(synchronize_session=False)
        db.query(BatchCampaignReviewer).filter(BatchCampaignReviewer.campaign_id.in_(ids)).delete(synchronize_session=False)
        db.query(BatchCampaign).filter(BatchCampaign.id.in_(ids)).delete(synchronize_session=False)
        db.query(User).filter(User.id.in_(user_ids)).delete(synchronize_session=False)
        db.commit()


def change(client, target, campaign, enabled, expected):
    return client.put(f'/admin/users/{target.id}/campaign-access/{campaign.id}', json={
        'enabled': enabled, 'expected_granted': expected,
    })


def test_grant_revoke_regrant_persists_and_limits_access(access_env):
    client, db, admin, target, campaigns = access_env
    external, own, other = campaigns
    actor = {'id': target.id, 'role': target.role, 'tenant_id': target.tenant_id}
    with pytest.raises(HTTPException):
        _campaign_or_404(db, external.id, actor)
    assert change(client, target, external, True, False).json()['granted']
    db.expire_all()
    assert _campaign_or_404(db, external.id, actor).id == external.id
    with pytest.raises(HTTPException):
        _campaign_or_404(db, other.id, actor)
    assert target.role == 'user' and target.tenant_id == own.tenant_id
    listed = client.get(f'/admin/users/{target.id}/campaign-access', params={'granted_only': True}).json()
    assert [c['id'] for c in listed['items']] == [external.id]
    # A stale administrator must not overwrite a newer change.
    assert change(client, target, external, True, False).status_code == 409
    assert change(client, target, external, False, True).status_code == 200
    db.expire_all()
    with pytest.raises(HTTPException):
        _campaign_or_404(db, external.id, actor)
    assert change(client, target, external, True, False).status_code == 200
    grant = db.get(BatchCampaignReviewer, (external.id, target.id))
    assert grant.granted_by == admin['id'] and grant.revoked_at is None
    assert db.query(AuditLog).filter_by(user_id=admin['id'], action='admin.user_campaign_access_changed').count() == 3


def test_only_admin_can_list_or_change_access(access_env):
    client, db, actor, target, campaigns = access_env
    actor.update(id=target.id, role='user')
    assert client.get(f'/admin/users/{target.id}/campaign-access').status_code == 403
    assert change(client, target, campaigns[0], True, False).status_code == 403
    assert db.query(BatchCampaignReviewer).filter_by(user_id=target.id).count() == 0


def test_inherited_access_and_filtered_pagination(access_env):
    client, db, actor, target, campaigns = access_env
    base = f'/admin/users/{target.id}/campaign-access'
    own = client.get(base, params={'search': campaigns[1].name}).json()['items'][0]
    assert own['inherited'] == 'team' and own['has_access'] and not own['granted']
    assert change(client, target, campaigns[1], True, False).status_code == 422
    assert client.get(base, params={'search': '%'}).json()['items'][0]['id'] == campaigns[2].id
    a = client.get(base, params={'search': target.tenant_id, 'limit': 1}).json()
    b = client.get(base, params={'search': target.tenant_id, 'limit': 1, 'offset': 1}).json()
    assert a['total'] == b['total'] == 3 and a['items'][0]['id'] != b['items'][0]['id']
    target.role = 'admin'; db.commit()
    assert client.get(base, params={'search': campaigns[0].name}).json()['items'][0]['inherited'] == 'admin'
    assert change(client, target, campaigns[0], True, False).status_code == 422


def test_revocation_survives_closed_campaign_disabled_user_and_feature(access_env, monkeypatch):
    client, db, actor, target, campaigns = access_env
    assert change(client, target, campaigns[0], True, False).status_code == 200
    campaigns[0].status = 'completed'; target.is_active = False; db.commit()
    monkeypatch.setenv('BATCH_CAMPAIGN_ENABLED', '0')
    listed = client.get(f'/admin/users/{target.id}/campaign-access', params={'granted_only': True}).json()
    assert listed['items'][0]['id'] == campaigns[0].id
    assert not listed['campaigns_enabled'] and not listed['user_active']
    assert change(client, target, campaigns[0], False, True).status_code == 200
    assert change(client, target, campaigns[2], True, False).status_code == 422
    target.is_active = True; db.commit()
    assert change(client, target, campaigns[2], True, False).status_code == 422


def test_invalid_targets_do_not_write(access_env):
    client, db, actor, target, campaigns = access_env
    campaigns[0].kind = 'art_track'; campaigns[2].status = 'completed'; db.commit()
    assert change(client, target, campaigns[0], True, False).status_code == 422
    assert change(client, target, campaigns[2], True, False).status_code == 422
    assert client.get('/admin/users/999999999/campaign-access').status_code == 404
    assert client.put(f'/admin/users/{target.id}/campaign-access/missing', json={'enabled': True, 'expected_granted': False}).status_code == 404
    assert db.query(BatchCampaignReviewer).filter_by(user_id=target.id).count() == 0
