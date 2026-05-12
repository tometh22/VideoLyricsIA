"""Tests del módulo drive_oauth + endpoints /drive/*.

Mocks Google API completamente — no pega a oauth2.googleapis.com.
Tests cubren:
- Encrypt/decrypt round-trip con Fernet
- State token sign/verify + expiración + tampering
- Endpoints /drive/auth-url, /drive/status, /drive/disconnect
- Callback con state válido / inválido / expirado
- Re-conexión sobrescribe row existente
"""
import os
import time
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

# Setear vars ANTES de importar drive_oauth (que las lee al import time)
os.environ.setdefault("DRIVE_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("GOOGLE_OAUTH_CLIENT_ID", "test-client-id.apps.googleusercontent.com")
os.environ.setdefault("GOOGLE_OAUTH_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("GOOGLE_OAUTH_REDIRECT_URI", "http://localhost:8000/drive/callback")

import drive_oauth  # noqa: E402
from drive_oauth import (
    DriveOAuthError,
    DriveTokenDecryptError,
    build_authorization_url,
    build_state_token,
    decrypt_token,
    encrypt_token,
    verify_state_token,
)
from database import UserDriveTokens  # noqa: E402


# ─── Encryption ────────────────────────────────────────────────────

def test_encrypt_decrypt_roundtrip():
    """Token plaintext debería volver igual tras encrypt → decrypt."""
    plaintext = "1//06pretend-refresh-token-from-google-xyz123"
    ciphertext = encrypt_token(plaintext)
    assert ciphertext != plaintext
    assert decrypt_token(ciphertext) == plaintext


def test_decrypt_with_wrong_key_raises(monkeypatch):
    """Si la encryption key cambió, decrypt debe fallar con error
    descriptivo (no silent return de basura)."""
    cipher = encrypt_token("test-token")
    # Rotar la key — Fernet usa el módulo-level _fernet() cacheado por
    # los args, pero como leemos la env var en cada call, basta con
    # cambiar la var.
    monkeypatch.setattr(drive_oauth, "DRIVE_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    with pytest.raises(DriveTokenDecryptError):
        decrypt_token(cipher)


def test_decrypt_with_corrupted_cipher_raises():
    with pytest.raises(DriveTokenDecryptError):
        decrypt_token("not-a-real-fernet-token")


# ─── State token (CSRF protection) ─────────────────────────────────

def test_state_token_roundtrip():
    state = build_state_token(user_id=42)
    assert verify_state_token(state) == 42


def test_state_token_rejects_tampered():
    state = build_state_token(user_id=42)
    # Cambiar 1 char rompe el HMAC
    tampered = state[:-3] + ("aaa" if state[-3:] != "aaa" else "bbb")
    with pytest.raises(DriveOAuthError):
        verify_state_token(tampered)


def test_state_token_rejects_expired(monkeypatch):
    """Mockeo time.time() para simular un state token de hace 1h."""
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() - 3600)
    state = build_state_token(user_id=42)
    monkeypatch.setattr(time, "time", real_time)
    with pytest.raises(DriveOAuthError):
        verify_state_token(state)


def test_state_token_rejects_wrong_type():
    """Un JWT con type != drive_oauth_state debería rechazarse incluso
    si está firmado (defensa contra reusar el JWT de auth normal)."""
    import jwt
    from auth import JWT_SECRET, JWT_ALGORITHM
    bad = jwt.encode(
        {"user_id": 42, "exp": int(time.time()) + 600, "type": "other"},
        JWT_SECRET, algorithm=JWT_ALGORITHM,
    )
    with pytest.raises(DriveOAuthError):
        verify_state_token(bad)


# ─── /drive/auth-url endpoint ─────────────────────────────────────

def test_auth_url_requires_login(client):
    res = client.get("/drive/auth-url")
    assert res.status_code == 401


def test_auth_url_returns_google_url_with_state(client, user_token):
    res = client.get("/drive/auth-url", headers={"Authorization": f"Bearer {user_token}"})
    assert res.status_code == 200
    body = res.json()
    assert "auth_url" in body
    url = body["auth_url"]
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth")
    assert "scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fdrive.file" in url
    assert "state=" in url
    assert "access_type=offline" in url


# ─── /drive/status endpoint ───────────────────────────────────────

def test_status_no_connection(client, user_token):
    """User que nunca conectó Drive → {connected: false}."""
    res = client.get("/drive/status", headers={"Authorization": f"Bearer {user_token}"})
    assert res.status_code == 200
    assert res.json() == {"connected": False}


def test_status_after_connection(client, user_token, db):
    """Con una row en user_drive_tokens, status debe reflejarlo."""
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {user_token}"}).json()
    row = UserDriveTokens(
        user_id=me["id"],
        encrypted_refresh_token=encrypt_token("test-refresh"),
        scope="https://www.googleapis.com/auth/drive.file",
        google_email="testuser@gmail.com",
    )
    db.add(row)
    db.commit()

    res = client.get("/drive/status", headers={"Authorization": f"Bearer {user_token}"})
    assert res.status_code == 200
    data = res.json()
    assert data["connected"] is True
    assert data["email"] == "testuser@gmail.com"


# ─── /drive/disconnect endpoint ───────────────────────────────────

def test_disconnect_when_not_connected(client, user_token):
    res = client.delete("/drive/disconnect", headers={"Authorization": f"Bearer {user_token}"})
    assert res.status_code == 200
    assert res.json() == {"ok": True, "was_connected": False}


def test_disconnect_removes_row_and_revokes(client, user_token, db):
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {user_token}"}).json()
    db.add(UserDriveTokens(
        user_id=me["id"],
        encrypted_refresh_token=encrypt_token("test-refresh-to-revoke"),
        scope="https://www.googleapis.com/auth/drive.file",
        google_email="x@y.com",
    ))
    db.commit()

    with patch("drive_oauth.revoke_refresh_token", return_value=True) as mock_revoke:
        res = client.delete("/drive/disconnect", headers={"Authorization": f"Bearer {user_token}"})

    assert res.status_code == 200
    assert res.json() == {"ok": True, "was_connected": True}
    mock_revoke.assert_called_once_with("test-refresh-to-revoke")

    db.expire_all()
    assert db.query(UserDriveTokens).filter(UserDriveTokens.user_id == me["id"]).first() is None


# ─── /drive/callback endpoint ─────────────────────────────────────

def test_callback_with_invalid_state_redirects_to_error(client):
    """State no firmado → callback no debería tirar 500, redirige a
    /settings?drive=error&reason=invalid_state."""
    res = client.get(
        "/drive/callback?code=fake&state=not-a-real-jwt",
        follow_redirects=False,
    )
    assert res.status_code == 302
    assert "drive=error" in res.headers["location"]
    assert "invalid_state" in res.headers["location"]


def test_callback_with_user_rejection_redirects(client):
    """Si el user rechazó en Google, viene `error=access_denied`."""
    res = client.get(
        "/drive/callback?error=access_denied",
        follow_redirects=False,
    )
    assert res.status_code == 302
    assert "drive=error" in res.headers["location"]
    assert "access_denied" in res.headers["location"]


def test_callback_happy_path_saves_tokens(client, user_token, db):
    """Mock Google exchange + userinfo, verificá que la row se crea
    con el refresh_token encriptado."""
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {user_token}"}).json()
    state = build_state_token(me["id"])

    fake_tokens = {
        "access_token": "ya29.fake-access",
        "refresh_token": "1//06fake-refresh-token-from-google",
        "scope": "https://www.googleapis.com/auth/drive.file",
        "expires_in": 3599,
        "token_type": "Bearer",
    }
    fake_userinfo = {"email": "alice@example.com", "name": "Alice"}

    with patch("drive_oauth.exchange_code_for_tokens", return_value=fake_tokens), \
         patch("drive_oauth.fetch_userinfo", return_value=fake_userinfo):
        res = client.get(
            f"/drive/callback?code=fake-code&state={state}",
            follow_redirects=False,
        )

    assert res.status_code == 302
    assert "drive=connected" in res.headers["location"]

    db.expire_all()
    row = db.query(UserDriveTokens).filter(UserDriveTokens.user_id == me["id"]).first()
    assert row is not None
    assert row.google_email == "alice@example.com"
    # No guardamos refresh plaintext
    assert "1//06fake-refresh-token-from-google" not in row.encrypted_refresh_token
    # Pero sí se decrypta correctamente
    assert decrypt_token(row.encrypted_refresh_token) == "1//06fake-refresh-token-from-google"


def test_callback_overwrites_existing_connection(client, user_token, db):
    """Re-conectar Drive (user revocó en Google y volvió) debe
    sobreescribir la row existente, no crear duplicado."""
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {user_token}"}).json()

    # Conexión inicial directa en DB (simula estado previo)
    db.add(UserDriveTokens(
        user_id=me["id"],
        encrypted_refresh_token=encrypt_token("old-refresh"),
        scope="...",
        google_email="old@example.com",
    ))
    db.commit()

    state = build_state_token(me["id"])
    fake_tokens = {
        "access_token": "ya29.new-access",
        "refresh_token": "new-refresh-token",
        "scope": "https://www.googleapis.com/auth/drive.file",
        "expires_in": 3599,
        "token_type": "Bearer",
    }
    with patch("drive_oauth.exchange_code_for_tokens", return_value=fake_tokens), \
         patch("drive_oauth.fetch_userinfo", return_value={"email": "new@example.com"}):
        client.get(f"/drive/callback?code=x&state={state}", follow_redirects=False)

    db.expire_all()
    rows = db.query(UserDriveTokens).filter(UserDriveTokens.user_id == me["id"]).all()
    assert len(rows) == 1, "no debe haber duplicado, debe overwrite"
    assert rows[0].google_email == "new@example.com"
    assert decrypt_token(rows[0].encrypted_refresh_token) == "new-refresh-token"
