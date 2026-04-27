"""Tests for authentication endpoints."""

from tests.conftest import auth


def test_health(client):
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


def test_plans_public(client):
    res = client.get("/plans")
    assert res.status_code == 200
    data = res.json()
    assert "100" in data
    assert "free" in data
    assert data["100"]["limit"] == 100


def test_register(client):
    res = client.post("/auth/register", json={
        "username": "newuser_auth",
        "password": "password123",
        "email": "newuser@test.com",
    })
    assert res.status_code == 200
    data = res.json()
    assert "token" in data
    assert data["user"]["username"] == "newuser_auth"
    assert data["user"]["plan"] == "free"
    assert data["user"]["email"] == "newuser@test.com"


def test_register_duplicate_username(client):
    client.post("/auth/register", json={
        "username": "dupuser",
        "password": "password123",
    })
    res = client.post("/auth/register", json={
        "username": "dupuser",
        "password": "password123",
    })
    assert res.status_code == 400


def test_register_short_password(client):
    res = client.post("/auth/register", json={
        "username": "shortpw",
        "password": "123",
    })
    assert res.status_code == 400
    assert "8 characters" in res.json()["detail"]


def test_register_short_username(client):
    res = client.post("/auth/register", json={
        "username": "ab",
        "password": "password123",
    })
    assert res.status_code == 400
    assert "3 characters" in res.json()["detail"]


def test_login_success(client):
    import uuid
    uname = f"logintest_{uuid.uuid4().hex[:6]}"
    client.post("/auth/register", json={
        "username": uname,
        "password": "password123",
    })
    res = client.post("/auth/login", json={
        "username": uname,
        "password": "password123",
    })
    assert res.status_code == 200
    assert "token" in res.json()


def test_login_wrong_password(client):
    res = client.post("/auth/login", json={
        "username": "admin",
        "password": "wrongpassword",
    })
    assert res.status_code == 401


def test_login_nonexistent_user(client):
    res = client.post("/auth/login", json={
        "username": "doesnotexist",
        "password": "password123",
    })
    assert res.status_code == 401


def test_me(client, user_token):
    res = client.get("/auth/me", headers=auth(user_token))
    assert res.status_code == 200
    assert "username" in res.json()


def test_me_no_auth(client):
    res = client.get("/auth/me")
    assert res.status_code == 403


def test_me_bad_token(client):
    res = client.get("/auth/me", headers=auth("invalid.token.here"))
    assert res.status_code == 401


def test_forgot_password(client):
    # Should always return OK (not leak email existence)
    res = client.post("/auth/forgot-password", json={
        "email": "nonexistent@test.com",
    })
    assert res.status_code == 200
    assert res.json()["ok"] is True


def test_usage(client, user_token):
    res = client.get("/usage", headers=auth(user_token))
    assert res.status_code == 200
    data = res.json()
    assert data["plan"] == "free"
    assert data["limit"] == 5
    assert data["used"] == 0
