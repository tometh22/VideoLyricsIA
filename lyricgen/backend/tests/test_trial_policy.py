import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

import trial_policy as policy
from database import CreditGrant, SessionLocal


@pytest.fixture
def trial(db, monkeypatch, admin_user_id):
    group = "trial_test_" + uuid.uuid4().hex[:12]
    monkeypatch.setenv("TRIAL_BILLING_GROUPS", group)
    identity = {"id": admin_user_id, "billing_group": group, "tenant_id": group, "plan": "free"}
    return identity


def test_trial_is_opt_in(db, monkeypatch):
    monkeypatch.delenv("TRIAL_BILLING_GROUPS", raising=False)
    assert not policy.applies({"billing_group": "universal"})
    assert policy.require_open(db, {"billing_group": "universal"}) is None


def test_pending_does_not_inherit_free_plan_or_old_bonus(db, trial):
    db.add(CreditGrant(billing_group=trial["billing_group"], amount=6, reason="old_bonus"))
    db.flush()
    from auth import get_plan_usage
    info = get_plan_usage(db, trial["id"], trial["tenant_id"], "free", trial["billing_group"])
    assert info["trial"]["state"] == "pending"
    assert info["total_available"] == 0
    with pytest.raises(HTTPException) as exc:
        policy.require_open(db, trial)
    assert exc.value.detail["code"] == "trial_not_started"


def test_explicit_activation_is_nine_credits_24h_and_idempotent(db, trial):
    first = policy.activate(db, trial["billing_group"], trial["id"])
    second = policy.activate(db, trial["billing_group"], trial["id"])
    assert first == second
    assert first["available"] == 9
    assert datetime.fromisoformat(first["expires_at"]) - datetime.fromisoformat(first["starts_at"]) == timedelta(hours=24)


def test_three_scenes_reserve_all_credits_and_rejection_cannot_refund(db, trial):
    policy.activate(db, trial["billing_group"], trial["id"])
    for i in range(3):
        policy.reserve(db, trial, f"trial_{i}", 3)
    assert policy.usage(db, trial["billing_group"])["total_available"] == 0
    with pytest.raises(HTTPException) as exc:
        policy.reserve(db, trial, "fourth", 3)
    assert exc.value.status_code == 402
    # Already reserved jobs may finish/retry without charging again.
    policy.reserve(db, trial, "trial_0", 3, purpose="retry")
    assert policy.snapshot(db, trial["billing_group"])["reserved"] == 9


def test_retry_budget_is_finite(db, trial):
    policy.activate(db, trial["billing_group"], trial["id"])
    for _ in range(3):
        policy.reserve(db, trial, "same", 3)
    with pytest.raises(HTTPException) as exc:
        policy.reserve(db, trial, "same", 3)
    assert exc.value.status_code == 429
    assert policy.snapshot(db, trial["billing_group"])["reserved"] == 3


def test_expiry_blocks_even_reserved_jobs_and_never_renews(db, trial):
    policy.activate(db, trial["billing_group"], trial["id"])
    policy.reserve(db, trial, "first", 3)
    grant = policy.latest_grant(db, trial["billing_group"])
    grant.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.flush()
    with pytest.raises(HTTPException) as exc:
        policy.reserve(db, trial, "first", 3)
    assert exc.value.detail["code"] == "trial_expired"
    assert policy.activate(db, trial["billing_group"], trial["id"])["state"] == "expired"


def test_reservation_rollback_does_not_spend(db, trial):
    policy.activate(db, trial["billing_group"], trial["id"])
    db.commit()
    policy.reserve(db, trial, "rollback", 3)
    db.rollback()
    assert policy.snapshot(db, trial["billing_group"])["reserved"] == 0


@pytest.mark.postgres
def test_concurrent_reservations_cannot_overspend_real_postgres(db, trial):
    if db.get_bind().dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL advisory lock")
    policy.activate(db, trial["billing_group"], trial["id"])
    policy.reserve(db, trial, "one", 3)
    policy.reserve(db, trial, "two", 3)
    db.commit()

    def attempt(job):
        with SessionLocal() as session:
            try:
                policy.reserve(session, trial, job, 3)
                session.commit()
                return 200
            except HTTPException as exc:
                session.rollback()
                return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(attempt, ["race-a", "race-b"]))
    assert sorted(result) == [200, 402]
    assert policy.snapshot(db, trial["billing_group"])["reserved"] == 9


def test_activation_route_is_admin_only(client, user_token, db, trial):
    response = client.post(f"/admin/trials/{trial['billing_group']}/activate",
                           headers={"Authorization": "Bearer " + user_token})
    assert response.status_code == 403
    assert policy.latest_grant(db, trial["billing_group"]) is None


def test_transcription_worker_denies_expired_trial_before_provider(monkeypatch):
    import transcription_worker
    def expired(_job_id):
        raise HTTPException(403, detail={"code": "trial_expired", "message": "expired"})
    monkeypatch.setattr(policy, "require_transcription_admitted", expired)
    monkeypatch.setattr(transcription_worker, "_fail", lambda job_id, message: {"job_id": job_id, "error": message})
    assert transcription_worker.run_transcription_job("expired", "/must-not-read.mp3") == {"job_id": "expired", "error": "expired"}


@pytest.fixture
def trial_account(client, db, user_token, monkeypatch):
    from database import User
    headers = {"Authorization": "Bearer " + user_token}
    me = client.get("/auth/me", headers=headers).json()
    user = db.query(User).filter(User.id == me["id"]).one()
    user.billing_group = "bounded_" + uuid.uuid4().hex[:12]
    monkeypatch.setenv("TRIAL_BILLING_GROUPS", user.billing_group)
    db.commit()
    identity = user.to_dict()
    policy.activate(db, user.billing_group, user.id)
    db.commit()
    return identity, headers


def test_scene_queue_failure_retains_one_durable_attempt(client, db, trial_account, monkeypatch):
    from database import AuditLog, JobOutboxEvent
    from tests.test_scenes_endpoints import _make_scene_job
    import main
    identity, headers = trial_account
    job_id = _make_scene_job(db, identity["tenant_id"], user_id=identity["id"])
    policy.reserve(db, identity, job_id, 3)
    db.commit()
    monkeypatch.setattr(main, "enqueue_edit", lambda **kw: (_ for _ in ()).throw(ConnectionError("offline")))
    response = client.post(f"/jobs/{job_id}/scenes/coro_1/regenerate", headers=headers, json={"hint": "red boat"})
    assert response.status_code == 202, response.text
    db.expire_all()
    events = db.query(JobOutboxEvent).filter(JobOutboxEvent.job_id == job_id,
                                            JobOutboxEvent.event_type == "edit.enqueue").all()
    assert len(events) == 1
    assert events[0].status == "pending"
    assert events[0].payload["edit_params"]["scene_hint"] == "red boat"
    assert sum(1 for (d,) in db.query(AuditLog.detail).filter(AuditLog.action == "job.scene_regenerate").all()
               if d.get("job_id") == job_id) == 1


def test_unreserved_scene_and_edit_rejected_before_publication(client, db, trial_account, monkeypatch):
    from tests.test_scenes_endpoints import _make_scene_job
    import main
    identity, headers = trial_account
    job_id = _make_scene_job(db, identity["tenant_id"], user_id=identity["id"])
    def forbidden(**kwargs):
        pytest.fail("unreserved job published")
    monkeypatch.setattr(main, "enqueue_edit", forbidden)
    for path, body in [(f"/jobs/{job_id}/scenes/coro_1/regenerate", {}),
                       (f"/edit/{job_id}", {"edit_type": "typography", "font": "anton"})]:
        response = client.post(path, headers=headers, json=body)
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "trial_job_not_reserved"


@pytest.mark.postgres
def test_concurrent_transcription_last_slot_real_postgres(db, trial):
    if db.get_bind().dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL advisory lock")
    policy.activate(db, trial["billing_group"], trial["id"])
    for index in range(5):
        policy.admit_transcription(db, trial, f"draft_{index}")
    db.commit()
    def attempt(job):
        with SessionLocal() as session:
            try:
                policy.admit_transcription(session, trial, job)
                session.commit()
                return 200
            except HTTPException as exc:
                session.rollback()
                return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(attempt, ["last-a", "last-b"]))
    assert sorted(result) == [200, 429]


def test_direct_transcribe_denies_capped_trial_before_provider(client, db, trial_account, monkeypatch, tmp_path):
    from tests.test_presigned_upload import _wav_bytes
    import main
    identity, headers = trial_account
    for index in range(6):
        policy.admit_transcription(db, identity, f"draft_{index}")
    db.commit()
    monkeypatch.setattr(main, "OUTPUTS_DIR", str(tmp_path))
    monkeypatch.setattr(main, "_enforce_disk_capacity", lambda: None)
    monkeypatch.setattr(main, "_enforce_memory_pressure", lambda: None)
    monkeypatch.setattr(main, "_validate_audio_file_on_disk", lambda *args: None)
    async def forbidden(*args, **kwargs):
        pytest.fail("unadmitted transcription called provider")
    monkeypatch.setattr(main, "_run_transcription_for_job", forbidden)
    response = client.post("/transcribe", headers=headers,
                           files={"file": ("capped.wav", _wav_bytes(), "audio/wav")})
    assert response.status_code == 429, response.text
    assert response.json()["detail"]["code"] == "trial_transcription_limit"
