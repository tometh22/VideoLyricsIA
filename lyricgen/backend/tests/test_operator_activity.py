from datetime import date, datetime, timezone
import uuid

import pytest
from fastapi import HTTPException
from database import AuditLog, BatchCampaign, Job, User
from operator_activity import collect, day_bounds


@pytest.fixture
def activity(db, monkeypatch):
    monkeypatch.setenv("BATCH_CAMPAIGN_ENABLED", "1")
    monkeypatch.setenv("BATCH_CAMPAIGN_SCOPES", "activity-test")
    owner = db.query(User).first()
    operator = User(username="activity-" + uuid.uuid4().hex, hashed_password="unused", tenant_id="activity-test", role="admin")
    db.add(operator); db.flush()
    user = {"id": operator.id, "role": "admin", "tenant_id": "activity-test"}
    campaign = BatchCampaign(id=uuid.uuid4().hex[:12], name="Campaña ajena", tenant_id="owner-test", created_by=owner.id,
                             kind="lyric_video", destination_portal="chile", status="active")
    db.add(campaign); db.flush()
    def audit(job, action, at, actor=None):
        db.add(AuditLog(user_id=actor or operator.id, action=action, detail={"job_id": job.job_id}, created_at=datetime.fromisoformat(at)))
        db.flush()
    def job():
        j = Job(job_id=uuid.uuid4().hex[:12], user_id=owner.id, tenant_id=campaign.tenant_id, campaign_id=campaign.id,
                filename="audio.wav", artist="Artista", song_title="Canción", status="pending_review")
        db.add(j); db.flush(); return j
    return user, owner, job, audit


def test_local_midnight_uses_half_open_utc_interval():
    assert day_bounds(date(2026, 11, 2)) == (datetime(2026, 11, 2, 3, tzinfo=timezone.utc), datetime(2026, 11, 3, 3, tzinfo=timezone.utc))


def test_actor_not_owner_and_edits_retries_formats_do_not_inflate(db, activity):
    user, owner, make, audit = activity
    j = make()
    audit(j, "campaign.creative.generation", "2026-11-02T12:00:00+00:00")
    audit(j, "campaign.creative.generation", "2026-11-02T12:01:00+00:00")
    audit(j, "campaign.creative.rendered", "2026-11-02T13:00:00+00:00", owner.id)
    audit(j, "campaign.creative.rendered", "2026-11-02T15:00:00+00:00", owner.id)
    audit(j, "job.edit_request", "2026-11-02T14:00:00+00:00")
    audit(j, "job.edit_request", "2026-11-02T14:01:00+00:00")
    failed = make()
    audit(failed, "admin.cross_tenant_access", "2026-11-02T14:01:00+00:00")
    waiting = make()
    audit(waiting, "campaign.creative.generation", "2026-11-02T14:01:00+00:00")
    result = collect(db, user, date(2026, 11, 2))
    assert (result['generated'], result['requested'], result['edited_songs'], result['edit_requests']) == (1, 2, 1, 2)
    assert result['pending_review_now'] == 1
    assert result['songs'][0]['job_id'] == j.job_id
    assert collect(db, {**user, 'id': owner.id}, date(2026, 11, 2))['generated'] == 0


def test_completion_day_and_successful_request_actor(db, activity):
    user, owner, make, audit = activity
    overnight = make()
    audit(overnight, 'campaign.creative.generation', '2026-11-02T02:00:00+00:00')
    audit(overnight, 'campaign.creative.rendered', '2026-11-02T03:00:00+00:00', owner.id)
    audit(overnight, 'campaign.creative.rendered', '2026-11-03T03:00:00+00:00', owner.id)
    other_actor = make()
    audit(other_actor, 'campaign.creative.generation', '2026-11-01T12:00:00+00:00', owner.id)
    audit(other_actor, 'campaign.creative.generation', '2026-11-02T12:00:00+00:00')
    audit(other_actor, 'campaign.creative.rendered', '2026-11-02T13:00:00+00:00', owner.id)
    already_rendered = make()
    audit(already_rendered, 'campaign.creative.generation', '2026-11-01T12:00:00+00:00', owner.id)
    audit(already_rendered, 'campaign.creative.rendered', '2026-11-01T13:00:00+00:00', owner.id)
    audit(already_rendered, 'campaign.creative.generation', '2026-11-02T12:00:00+00:00')
    audit(already_rendered, 'campaign.creative.rendered', '2026-11-02T13:00:00+00:00', owner.id)
    tomorrow = make()
    audit(tomorrow, 'campaign.creative.generation', '2026-11-02T14:00:00+00:00')
    audit(tomorrow, 'campaign.creative.rendered', '2026-11-03T03:00:00+00:00', owner.id)
    result = collect(db, user, date(2026, 11, 2))
    assert result['generated'] == 2 and result['requested'] == 1
    assert {row['job_id'] for row in result['songs']} == {overnight.job_id, other_actor.job_id}
    assert collect(db, user, date(2026, 11, 3))['generated'] == 1


def test_revoked_campaign_access_hides_names_counts_and_edits(db, activity):
    user, owner, make, audit = activity
    j = make()
    audit(j, 'campaign.creative.generation', '2026-11-02T12:00:00+00:00')
    audit(j, 'campaign.creative.rendered', '2026-11-02T13:00:00+00:00', owner.id)
    audit(j, 'job.edit_request', '2026-11-02T14:00:00+00:00')
    result = collect(db, {**user, 'role': 'user'}, date(2026, 11, 2))
    assert result['songs'] == [] and result['campaigns'] == []
    assert result['generated'] == result['requested'] == result['edited_songs'] == 0


def test_feature_gate(db, activity, monkeypatch):
    user, *_ = activity
    monkeypatch.setenv('BATCH_CAMPAIGN_ENABLED', '0')
    with pytest.raises(HTTPException) as exc:
        collect(db, user, date(2026, 11, 2))
    assert exc.value.status_code == 404
