"""Trial exclusions for batch credentials and admission outside JWT auth."""
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

import batch_campaigns as batch
from database import BatchCampaign, BatchUploadSession, Job, JobOutboxEvent, User


@pytest.fixture
def batch_account(db, monkeypatch):
    suffix = uuid.uuid4().hex
    group = "bounded-" + suffix
    monkeypatch.setenv("TRIAL_BILLING_GROUPS", group)
    monkeypatch.setenv("BATCH_CAMPAIGN_ENABLED", "1")
    # Endpoint commits stay within the fixture's rolled-back transaction.
    monkeypatch.setattr(db, "commit", db.flush)
    owner = User(username="batch-owner-" + suffix, hashed_password="unused",
                 tenant_id=group, billing_group="ordinary-" + suffix)
    issuer = User(username="batch-admin-" + suffix, hashed_password="unused",
                  tenant_id="platform", role="admin", billing_group=None)
    db.add_all([owner, issuer])
    db.flush()
    campaign = BatchCampaign(id=suffix[:12], tenant_id=group, created_by=owner.id,
                             name="Batch trial policy", status="active")
    db.add(campaign)
    db.flush()
    token, code = "upload-" + suffix, suffix[:12].upper()
    session = BatchUploadSession(
        id=str(uuid.uuid4()), campaign_id=campaign.id, tenant_id=group,
        created_by=issuer.id, code_hash=batch._hash_secret(code),
        code_expires_at=batch._now() + timedelta(minutes=5),
        token_hash=batch._hash_secret(token),
        token_expires_at=batch._now() + timedelta(hours=12),
    )
    db.add(session)
    db.flush()
    return SimpleNamespace(owner=owner, issuer=issuer, campaign=campaign,
                           session=session, token=token, code=code, group=group)


def _opt_in(db, account, who="owner"):
    getattr(account, who).billing_group = account.group
    db.flush()


def _denied(call):
    with pytest.raises(HTTPException) as exc:
        call()
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "trial_feature_unavailable"


@pytest.mark.parametrize("who", ["owner", "issuer"])
def test_preexisting_pair_code_denied_after_owner_or_issuer_opt_in(db, batch_account, who):
    a = batch_account
    _opt_in(db, a, who)
    original_hash = a.session.token_hash
    _denied(lambda: batch.exchange_upload_session(
        batch.PairExchange(campaign_id=a.campaign.id, code=a.code), db))
    assert a.session.claimed_at is None
    assert a.session.token_hash == original_hash


@pytest.mark.parametrize("who", ["owner", "issuer"])
@pytest.mark.parametrize("operation", ["manifest", "ticket", "complete"])
def test_existing_upload_token_cannot_mutate_after_opt_in(db, batch_account, who, operation):
    a = batch_account
    _opt_in(db, a, who)
    # These handlers must deny before reading the manifest/item or storage.
    calls = {
        "manifest": lambda: batch.register_manifest(a.campaign.id, None, a.token, db),
        "ticket": lambda: batch.campaign_upload_ticket("missing-item", a.token, db),
        "complete": lambda: batch.campaign_upload_complete(
            "missing-item", batch.UploadComplete(), a.token, db),
    }
    _denied(calls[operation])
    assert a.session.revoked_at is None


def test_trial_token_keeps_read_only_inspection(db, batch_account):
    a = batch_account
    _opt_in(db, a)
    result = batch.inspect_upload_session(a.token, db)
    assert result["campaign_id"] == a.campaign.id


def test_normal_group_keeps_exchange_and_upload_authorization(db, batch_account):
    a = batch_account
    result = batch.exchange_upload_session(
        batch.PairExchange(campaign_id=a.campaign.id, code=a.code), db)
    assert batch._upload_session_or_401(
        db, result["upload_token"], a.campaign.id) is a.session


def test_disabled_policy_does_not_query_or_change_normal_admission(monkeypatch):
    monkeypatch.delenv("TRIAL_BILLING_GROUPS", raising=False)
    db = Mock()
    assert batch._has_trial_batch_owner(db, 123) is False
    batch._require_non_trial_upload_session(db, SimpleNamespace())
    db.query.assert_not_called()


def test_admin_cannot_issue_new_token_for_trial_campaign(db, batch_account):
    a = batch_account
    _opt_in(db, a)
    actor = a.issuer.to_dict()
    _denied(lambda: batch.create_upload_session(a.campaign.id, actor, db))


def test_trial_campaign_cannot_admit_initial_or_separated_transcription(db, batch_account):
    a = batch_account
    _opt_in(db, a)
    assert batch._promote_campaign(db, a.campaign) == []
    assert batch._queue_full_stage_for_separated(db, a.campaign, room=3) == []
    _denied(lambda: batch._create_stage_event(
        db, a.campaign, None, pipeline_stage="full"))
    assert db.query(Job).filter(Job.campaign_id == a.campaign.id).count() == 0
    assert db.query(JobOutboxEvent).join(Job, Job.job_id == JobOutboxEvent.job_id).filter(
        Job.campaign_id == a.campaign.id).count() == 0


def test_admin_cannot_retry_trial_campaign_item(db, batch_account):
    a = batch_account
    _opt_in(db, a)
    _denied(lambda: batch.retry_campaign_item(
        a.campaign.id, "missing-item", a.issuer.to_dict(), db))


def test_trial_campaign_render_admission_denied(db, batch_account, monkeypatch):
    a = batch_account
    _opt_in(db, a)
    monkeypatch.setattr(batch, "require_prebackground_approval", lambda job: None)
    job = SimpleNamespace(workload_class="batch", campaign_id=a.campaign.id,
                          user_id=a.owner.id)
    _denied(lambda: batch.enforce_render_capacity(db, job))


@pytest.mark.parametrize("kind", ["lyric_video", "art_track"])
def test_reconciler_skips_trial_but_continues_normal_campaign(kind, monkeypatch):
    trial = SimpleNamespace(created_by=1, kind=kind, id="trial")
    normal = SimpleNamespace(created_by=2, kind="lyric_video", id="normal")
    db = Mock()
    db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [trial, normal]
    monkeypatch.setattr(batch, "feature_enabled", lambda: True)
    monkeypatch.setattr(batch, "SessionLocal", lambda: db)
    monkeypatch.setattr(batch, "_has_trial_batch_owner", lambda db, owner: owner == 1)
    promote = Mock(return_value=[])
    monkeypatch.setattr(batch, "_promote_campaign", promote)
    monkeypatch.setattr(batch, "_campaign_rows", lambda db, campaign_id: [])
    assert batch.reconcile_batch_campaigns() == {"promoted": 0, "dispatched": 0}
    promote.assert_called_once_with(db, normal)
    db.commit.assert_not_called()
