"""Security invariants for revocable access tokens and login sessions."""

import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from jose import jwt
from sqlalchemy.exc import SQLAlchemyError

import auth as auth_module
from auth import decode_token, invalidate_user_access, start_login_session
from database import LoginSession, User
from tests.conftest import auth


def test_access_token_carries_type_version_and_persisted_jti(db, user_token):
    payload = decode_token(user_token)
    assert payload["tt"] == "access"
    assert payload["av"] == 0
    assert payload.get("jti")
    session = db.query(LoginSession).filter(LoginSession.jti == payload["jti"]).first()
    assert session is not None
    assert session.user_id == int(payload["sub"])


def test_missing_login_session_is_rejected(client, db, user_token):
    payload = decode_token(user_token)
    db.query(LoginSession).filter(LoginSession.jti == payload["jti"]).delete()
    db.commit()

    response = client.get("/auth/me", headers=auth(user_token))
    assert response.status_code == 401


def test_auth_version_bump_invalidates_existing_token(client, db, user_token):
    payload = decode_token(user_token)
    user = db.query(User).filter(User.id == int(payload["sub"])).first()
    user.auth_version += 1
    db.commit()

    response = client.get("/auth/me", headers=auth(user_token))
    assert response.status_code == 401


def test_media_token_cannot_authenticate_as_access_token(client, db, user_token):
    payload = decode_token(user_token)
    user = db.query(User).filter(User.id == int(payload["sub"])).first()
    media_token = auth_module.create_media_token(user, "job123", "preview")

    response = client.get("/auth/me", headers=auth(media_token))
    assert response.status_code == 401


def test_media_token_is_invalid_after_auth_version_bump(db, user_token):
    payload = decode_token(user_token)
    user = db.query(User).filter(User.id == int(payload["sub"])).first()
    media_token = auth_module.create_media_token(user, "job123", "preview")
    user.auth_version += 1
    db.commit()
    with pytest.raises(HTTPException) as exc:
        auth_module.verify_media_token(media_token, "job123", "preview", db)
    assert exc.value.status_code == 401


def test_legacy_version_zero_token_with_session_remains_valid(client, db, user_token):
    current = decode_token(user_token)
    user = db.query(User).filter(User.id == int(current["sub"])).first()
    legacy = jwt.encode(
        {
            "sub": str(user.id),
            "username": user.username,
            "role": user.role,
            "tenant_id": user.tenant_id,
            "plan": user.plan_id,
            "jti": current["jti"],
            "iat": time.time(),
            "exp": time.time() + 300,
        },
        auth_module.JWT_SECRET,
        algorithm=auth_module.JWT_ALGORITHM,
    )

    response = client.get("/auth/me", headers=auth(legacy))
    assert response.status_code == 200


def test_start_login_session_never_returns_token_when_persistence_fails():
    class FailingDb:
        rolled_back = False

        def add(self, _row):
            return None

        def commit(self):
            raise SQLAlchemyError("login_sessions unavailable")

        def rollback(self):
            self.rolled_back = True

    user = SimpleNamespace(
        id=1,
        username="secure-user",
        role="user",
        tenant_id="tenant",
        plan_id="free",
        auth_version=0,
    )
    db = FailingDb()

    with pytest.raises(HTTPException) as exc:
        start_login_session(db, user)
    assert exc.value.status_code == 503
    assert db.rolled_back is True


def test_session_validation_db_failure_is_503():
    class FailingDb:
        rolled_back = False

        def query(self, _model):
            raise SQLAlchemyError("login_sessions unavailable")

        def rollback(self):
            self.rolled_back = True

    db = FailingDb()
    with pytest.raises(HTTPException) as exc:
        auth_module._validate_login_session(db, SimpleNamespace(id=1), "jti")
    assert exc.value.status_code == 503
    assert db.rolled_back is True


def test_invalidation_bumps_version_and_revokes_other_sessions(db, user_token):
    payload = decode_token(user_token)
    user = db.query(User).filter(User.id == int(payload["sub"])).first()
    other = LoginSession(user_id=user.id, jti="other-session")
    db.add(other)
    db.commit()

    version, revoked = invalidate_user_access(db, user, keep_jti=payload["jti"])
    db.commit()

    assert version == 1
    assert revoked == 1
    current = db.query(LoginSession).filter(LoginSession.jti == payload["jti"]).first()
    assert current.revoked_at is None
    assert db.query(LoginSession).filter(LoginSession.jti == "other-session").first().revoked_at is not None


def test_authenticated_password_change_rotates_token_and_keeps_current_session(client, db):
    username = f"pwchange-{int(time.time() * 1000000)}"
    registered = client.post("/auth/register", json={
        "username": username,
        "password": "old-password-123",
        "email": f"{username}@example.test",
    })
    old_token = registered.json()["token"]
    old_payload = decode_token(old_token)

    changed = client.post(
        "/auth/change-password",
        json={"current_password": "old-password-123", "new_password": "new-password-456"},
        headers=auth(old_token),
    )
    assert changed.status_code == 200, changed.text
    new_token = changed.json()["token"]
    new_payload = decode_token(new_token)
    assert new_payload["jti"] == old_payload["jti"]
    assert new_payload["av"] == old_payload["av"] + 1
    assert client.get("/auth/me", headers=auth(old_token)).status_code == 401
    assert client.get("/auth/me", headers=auth(new_token)).status_code == 200


def test_password_reset_revokes_every_login_session(client, db):
    username = f"pwreset-{int(time.time() * 1000000)}"
    registered = client.post("/auth/register", json={
        "username": username,
        "password": "old-password-123",
        "email": f"{username}@example.test",
    })
    old_token = registered.json()["token"]
    user = db.query(User).filter(User.username == username).first()
    reset_token = auth_module.create_password_reset_token(db, user)

    reset = client.post("/auth/reset-password", json={
        "token": reset_token,
        "password": "reset-password-789",
    })
    assert reset.status_code == 200, reset.text
    assert client.get("/auth/me", headers=auth(old_token)).status_code == 401
    db.expire_all()
    rows = db.query(LoginSession).filter(LoginSession.user_id == user.id).all()
    assert rows and all(row.revoked_at is not None for row in rows)


def test_global_logout_bumps_versions_without_rotating_secret(client, admin_token, user_token):
    secret_before = auth_module.JWT_SECRET
    response = client.post(
        "/admin/ops/logout-all",
        json={"confirmation": "LOGOUT_ALL_USERS", "reason": "security drill"},
        headers=auth(admin_token),
    )
    assert response.status_code == 200, response.text
    assert response.json()["users_updated"] >= 2
    assert auth_module.JWT_SECRET == secret_before
    assert client.get("/auth/me", headers=auth(user_token)).status_code == 401
    assert client.get("/auth/me", headers=auth(admin_token)).status_code == 401
