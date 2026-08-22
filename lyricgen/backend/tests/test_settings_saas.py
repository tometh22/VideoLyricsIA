"""Tests de Configuración SaaS: perfil, sesiones de login, mi equipo."""
import uuid as _uuid

from tests.conftest import auth
from database import AuditLog, EmailVerificationToken, User, LoginSession


def _delete_users(db, user_ids):
    """Delete PostgreSQL FK children before disposable test users."""
    user_ids = list(user_ids)
    if not user_ids:
        return
    db.query(LoginSession).filter(LoginSession.user_id.in_(user_ids)).delete(
        synchronize_session=False,
    )
    db.query(AuditLog).filter(AuditLog.user_id.in_(user_ids)).delete(
        synchronize_session=False,
    )
    db.query(EmailVerificationToken).filter(
        EmailVerificationToken.user_id.in_(user_ids),
    ).delete(synchronize_session=False)
    db.query(User).filter(User.id.in_(user_ids)).delete(synchronize_session=False)
    db.commit()


def _register(client):
    username = f"saas_{_uuid.uuid4().hex[:6]}"
    res = client.post("/auth/register", json={
        "username": username, "password": "testpass12345",
        "email": f"{username}@test.com",
    })
    return res.json()["token"], username


# --- Perfil -----------------------------------------------------------------

def test_update_profile_full_name(client):
    token, _ = _register(client)
    res = client.patch("/auth/profile", headers=auth(token), json={"full_name": "Ana Patricia M."})
    assert res.status_code == 200
    assert res.json()["full_name"] == "Ana Patricia M."
    # /auth/me lo refleja
    me = client.get("/auth/me", headers=auth(token)).json()
    assert me["full_name"] == "Ana Patricia M."


def test_avatar_rejects_bad_mime(client):
    token, _ = _register(client)
    res = client.post("/auth/avatar", headers=auth(token),
                      files={"file": ("x.txt", b"hello", "text/plain")})
    assert res.status_code == 400


# --- Sesiones de login ------------------------------------------------------

def test_login_creates_session(client, db):
    token, username = _register(client)
    me = client.get("/auth/me", headers=auth(token)).json()
    # El registro abrió una sesión
    res = client.get("/auth/sessions", headers=auth(token))
    assert res.status_code == 200
    sessions = res.json()["sessions"]
    assert len(sessions) >= 1
    assert any(s["current"] for s in sessions)
    _delete_users(db, [me["id"]])


def test_revoke_session_blocks_token(client, db):
    # Login dos veces = dos sesiones (dos jti distintos)
    token1, username = _register(client)
    me = client.get("/auth/me", headers=auth(token1)).json()
    login2 = client.post("/auth/login", json={"username": username, "password": "testpass12345"})
    token2 = login2.json()["token"]
    try:
        # token1 lista 2 sesiones; revoca la que NO es la actual (la de token2)
        sessions = client.get("/auth/sessions", headers=auth(token1)).json()["sessions"]
        assert len(sessions) == 2
        other = next(s for s in sessions if not s["current"])
        rev = client.post(f"/auth/sessions/{other['id']}/revoke", headers=auth(token1))
        assert rev.status_code == 200
        # token2 ahora queda 401
        assert client.get("/auth/me", headers=auth(token2)).status_code == 401
        # token1 sigue vivo
        assert client.get("/auth/me", headers=auth(token1)).status_code == 200
    finally:
        _delete_users(db, [me["id"]])


def test_revoke_others(client, db):
    token1, username = _register(client)
    me = client.get("/auth/me", headers=auth(token1)).json()
    token2 = client.post("/auth/login", json={"username": username, "password": "testpass12345"}).json()["token"]
    try:
        res = client.post("/auth/sessions/revoke-others", headers=auth(token2))
        assert res.status_code == 200
        assert res.json()["revoked_count"] >= 1
        # token2 (la actual) sigue viva, token1 muere
        assert client.get("/auth/me", headers=auth(token2)).status_code == 200
        assert client.get("/auth/me", headers=auth(token1)).status_code == 401
    finally:
        _delete_users(db, [me["id"]])


def test_sessions_require_auth(client):
    assert client.get("/auth/sessions").status_code in (401, 403)


# --- Mi equipo --------------------------------------------------------------

def test_team_members_same_tenant(client, admin_token, db):
    # Crear 2 usuarios en el mismo tenant
    suffix = _uuid.uuid4().hex[:6]
    for n in ("one", "two"):
        client.post("/admin/users", headers=auth(admin_token), json={
            "username": f"team_{n}_{suffix}", "password": "testpass12345",
            "email": f"{n}_{suffix}@test.com", "tenant_id": f"team_{suffix}",
        })
    login = client.post("/auth/login", json={"username": f"team_one_{suffix}", "password": "testpass12345"})
    token = login.json()["token"]
    try:
        res = client.get("/team/members", headers=auth(token))
        assert res.status_code == 200
        data = res.json()
        assert data["tenant_id"] == f"team_{suffix}"
        usernames = {m["username"] for m in data["members"]}
        assert f"team_one_{suffix}" in usernames and f"team_two_{suffix}" in usernames
        assert any(m["is_self"] for m in data["members"])
    finally:
        user_ids = [row[0] for row in db.query(User.id).filter(
            User.tenant_id == f"team_{suffix}",
        ).all()]
        _delete_users(db, user_ids)
