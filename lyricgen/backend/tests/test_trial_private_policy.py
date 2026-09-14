"""Private installation invitation guards; no provider/storage calls."""
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

import database
import trial_policy as policy


@pytest.mark.parametrize("value,expected", [
    ("", False), ("0", False), ("false", False), ("off", False),
    ("1", True), ("true", True), ("yes", True), ("on", True), (" TRUE ", True),
])
def test_private_mode_requires_explicit_flag(monkeypatch, value, expected):
    monkeypatch.setenv("TRIAL_ONLY_MODE", value)
    assert policy.private_only() is expected


@pytest.mark.parametrize("identity", [
    None, {}, {"role": "user"}, {"role": "user", "billing_group": "other"},
    {"role": "admin_backup", "billing_group": "other"},
])
@pytest.mark.parametrize("groups", ["", "invited"])
def test_private_mode_denies_uninvited_identity(monkeypatch, identity, groups):
    monkeypatch.setenv("TRIAL_ONLY_MODE", "1")
    monkeypatch.setenv("TRIAL_BILLING_GROUPS", groups)
    with pytest.raises(HTTPException) as exc:
        policy.require_invited(identity)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "trial_invitation_required"
    assert "invitación" in exc.value.detail["message"]


def test_invited_group_and_admin_are_allowed_but_flag_off_needs_no_invitation(monkeypatch):
    monkeypatch.setenv("TRIAL_ONLY_MODE", "1")
    monkeypatch.setenv("TRIAL_BILLING_GROUPS", " invited,another ")
    policy.require_invited({"role": "user", "billing_group": "invited"})
    monkeypatch.setenv("TRIAL_BILLING_GROUPS", "")
    policy.require_invited({"role": "admin"})
    monkeypatch.delenv("TRIAL_ONLY_MODE", raising=False)
    policy.require_invited({"role": "user", "billing_group": "public"})


def _source_owner(monkeypatch, identity):
    db = Mock()
    job = SimpleNamespace(user_id=7)
    owner = SimpleNamespace(to_dict=lambda: identity)
    db.query.return_value.filter.return_value.first.side_effect = [job, owner]
    monkeypatch.setattr(database, "SessionLocal", lambda: nullcontext(db))
    return db


@pytest.mark.parametrize("gate", [policy.require_job_open, policy.require_job_reserved,
                                   policy.require_transcription_admitted])
@pytest.mark.parametrize("groups", ["", "invited"])
def test_worker_checks_source_owner_invitation_before_clock(monkeypatch, gate, groups):
    monkeypatch.setenv("TRIAL_ONLY_MODE", "1")
    monkeypatch.setenv("TRIAL_BILLING_GROUPS", groups)
    db = _source_owner(monkeypatch, {"id": 7, "role": "user", "billing_group": "public"})
    clock = Mock(side_effect=AssertionError("uninvited owner must fail before clock"))
    monkeypatch.setattr(policy, "require_open", clock)
    with pytest.raises(HTTPException) as exc:
        gate("job")
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "trial_invitation_required"
    clock.assert_not_called()
    assert db.query.call_count == 2


@pytest.mark.parametrize("gate", [policy.require_job_open, policy.require_job_reserved,
                                   policy.require_transcription_admitted])
def test_private_invitation_does_not_waive_trial_clock(monkeypatch, gate):
    monkeypatch.setenv("TRIAL_ONLY_MODE", "1")
    monkeypatch.setenv("TRIAL_BILLING_GROUPS", "invited")
    _source_owner(monkeypatch, {"id": 7, "role": "admin", "billing_group": "invited"})
    monkeypatch.setattr(policy, "require_open", Mock(side_effect=HTTPException(
        403, detail={"code": "trial_expired", "message": "expired"})))
    with pytest.raises(HTTPException) as exc:
        gate("job")
    assert exc.value.detail["code"] == "trial_expired"


@pytest.mark.parametrize("gate", [policy.require_job_open, policy.require_job_reserved,
                                   policy.require_transcription_admitted])
def test_normal_environment_fastpath_and_private_database_failure(monkeypatch, gate):
    monkeypatch.delenv("TRIAL_ONLY_MODE", raising=False)
    monkeypatch.delenv("TRIAL_BILLING_GROUPS", raising=False)
    session = Mock(side_effect=RuntimeError("database unavailable"))
    monkeypatch.setattr(database, "SessionLocal", session)
    gate("job")
    session.assert_not_called()
    monkeypatch.setenv("TRIAL_ONLY_MODE", "1")
    with pytest.raises(RuntimeError, match="database unavailable"):
        gate("job")


@pytest.fixture
def preissued_identity(client, monkeypatch):
    """A genuine public account/token created before private mode is enabled."""
    import uuid

    monkeypatch.delenv("TRIAL_ONLY_MODE", raising=False)
    monkeypatch.delenv("TRIAL_BILLING_GROUPS", raising=False)
    username = "private_test_" + uuid.uuid4().hex[:10]
    credentials = {"username": username, "password": "private-test-password-123"}
    response = client.post("/auth/register", json={
        **credentials, "email": username + "@test.com",
    })
    assert response.status_code == 200
    data = response.json()
    return {"credentials": credentials, "id": data["user"]["id"],
            "headers": {"Authorization": "Bearer " + data["token"]}}


def test_private_signup_is_blocked_before_any_user_is_created(client, db, monkeypatch):
    import uuid

    monkeypatch.setenv("TRIAL_ONLY_MODE", "1")
    monkeypatch.delenv("TRIAL_BILLING_GROUPS", raising=False)
    username = "private_denied_" + uuid.uuid4().hex[:10]
    before = db.query(database.User).count()
    response = client.post("/auth/register", json={
        "username": username, "password": "private-test-password-123",
        "email": username + "@test.com",
    })
    assert response.status_code == 403
    assert "token" not in response.json()
    assert db.query(database.User).count() == before
    assert db.query(database.User).filter(database.User.username == username).first() is None


@pytest.mark.parametrize("groups", ["", "invited-only"])
def test_preissued_public_jwt_cannot_read_usage_or_upload(client, db, monkeypatch, preissued_identity, groups):
    import main

    monkeypatch.setenv("TRIAL_ONLY_MODE", "1")
    monkeypatch.setenv("TRIAL_BILLING_GROUPS", groups)
    upload_write = Mock(side_effect=AssertionError("uninvited request must never write upload"))
    monkeypatch.setattr(main, "_stream_upload_to_disk", upload_write)
    before = db.query(database.Job).count()
    responses = [
        client.get("/usage", headers=preissued_identity["headers"]),
        client.post("/upload", headers=preissued_identity["headers"],
                    data={"artist": "Private trial test", "style": "oscuro"},
                    files={"file": ("denied.mp3", b"not-read", "audio/mpeg")}),
    ]
    for response in responses:
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "trial_invitation_required"
    upload_write.assert_not_called()
    assert db.query(database.Job).count() == before


def test_valid_public_credentials_cannot_login_to_private_trial(client, monkeypatch, preissued_identity):
    monkeypatch.setenv("TRIAL_ONLY_MODE", "1")
    monkeypatch.setenv("TRIAL_BILLING_GROUPS", "invited-only")
    response = client.post("/auth/login", json=preissued_identity["credentials"])
    assert response.status_code == 403
    assert isinstance(response.json()["detail"], str)  # existing login UI contract
    assert "invitación" in response.json()["detail"]
    assert "token" not in response.json()


def test_invited_pending_user_can_read_usage_with_preissued_token(client, db, monkeypatch, preissued_identity):
    import uuid

    group = "private_pending_" + uuid.uuid4().hex[:10]
    owner = db.query(database.User).filter(database.User.id == preissued_identity["id"]).one()
    owner.billing_group = group
    db.commit()  # HTTP request uses its own session and must see this invitation
    monkeypatch.setenv("TRIAL_ONLY_MODE", "1")
    monkeypatch.setenv("TRIAL_BILLING_GROUPS", group)
    response = client.get("/usage", headers=preissued_identity["headers"])
    assert response.status_code == 200
    assert response.json()["trial"]["state"] == "pending"
    assert response.json()["total_available"] == 0
    assert policy.latest_grant(db, group) is None  # reading never activates


def test_admin_can_login_and_read_usage_in_private_mode_with_empty_invite_list(client, monkeypatch):
    import cache

    monkeypatch.setenv("TRIAL_ONLY_MODE", "1")
    monkeypatch.delenv("TRIAL_BILLING_GROUPS", raising=False)
    monkeypatch.setattr(cache, "get_or_set_json", lambda key, *, ttl_s, compute: compute())
    login = client.post("/auth/login", json={"username": "admin", "password": "testadmin123"})
    assert login.status_code == 200
    response = client.get("/usage", headers={"Authorization": "Bearer " + login.json()["token"]})
    assert response.status_code == 200


@pytest.mark.parametrize("gate", [policy.require_job_open, policy.require_job_reserved,
                                   policy.require_transcription_admitted])
def test_private_admin_worker_without_trial_membership_is_allowed(monkeypatch, gate):
    monkeypatch.setenv("TRIAL_ONLY_MODE", "1")
    monkeypatch.delenv("TRIAL_BILLING_GROUPS", raising=False)
    _source_owner(monkeypatch, {"id": 7, "role": "admin", "billing_group": None})
    gate("job")


def test_preissued_media_token_blocks_uninvited_but_allows_invited_expired_read(
    db, monkeypatch, preissued_identity,
):
    import uuid
    from datetime import datetime, timedelta, timezone
    import auth as auth_module

    owner = db.query(database.User).filter(database.User.id == preissued_identity["id"]).one()
    token = auth_module.create_media_token(owner, "media-job", "preview")
    group = "private_media_" + uuid.uuid4().hex[:10]
    monkeypatch.setenv("TRIAL_ONLY_MODE", "1")
    monkeypatch.setenv("TRIAL_BILLING_GROUPS", group)
    with pytest.raises(HTTPException) as exc:
        auth_module.verify_media_token(token, "media-job", "preview", db)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "trial_invitation_required"

    owner.billing_group = group
    now = datetime.now(timezone.utc)
    db.add(database.CreditGrant(
        billing_group=group, amount=9, reason=policy.ACTIVATION_REASON,
        granted_at=now - timedelta(hours=25), expires_at=now - timedelta(hours=1),
        revoked=False,
    ))
    db.flush()
    assert policy.snapshot(db, group)["state"] == "expired"
    db.commit()
    # Invitation is refreshed from the owner; trial expiry stops writes, not
    # reads of existing media. The original token remains otherwise valid.
    # Match the endpoint's fresh scoped_db session. Earlier resilience tests
    # reload database, leaving two mapped User classes in one test session;
    # reusing that identity map can retain the pre-invitation auth.User row.
    with database.SessionLocal() as media_db:
        verified = auth_module.verify_media_token(token, "media-job", "preview", media_db)
    assert verified["id"] == owner.id
