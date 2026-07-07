"""Self-service YouTube channel connections (OAuth per tenant).

Covers: token encryption at rest, the OAuth connect/callback round-trip
(state JWT validation, upsert-on-reconnect, first-channel-is-default),
tenant isolation, disconnect semantics, and channel resolution for
uploads. Google is never called — Flow/discovery seams are monkeypatched.
"""

import uuid
from datetime import datetime, timezone

import pytest

from tests.conftest import auth


# ─── token_crypto ────────────────────────────────────────────────────

def test_encrypt_decrypt_roundtrip():
    from token_crypto import encrypt_token, decrypt_token

    data = {"token": "at", "refresh_token": "rt", "scopes": ["a", "b"]}
    blob = encrypt_token(data)
    assert "rt" not in blob  # ciphertext, not plaintext
    assert decrypt_token(blob) == data


def test_decrypt_tampered_blob_raises():
    from cryptography.fernet import InvalidToken
    from token_crypto import encrypt_token, decrypt_token

    blob = encrypt_token({"token": "x"})
    with pytest.raises(InvalidToken):
        decrypt_token(blob[:-4] + "AAAA")


# ─── Fixtures / fakes ────────────────────────────────────────────────

def _register(client):
    from database import SessionLocal, User

    username = f"chuser_{uuid.uuid4().hex[:6]}"
    res = client.post("/auth/register", json={
        "username": username,
        "password": "testpass12345",
        "email": f"{username}@test.com",
    })
    assert res.status_code == 200, res.text
    token = res.json()["token"]
    s = SessionLocal()
    try:
        u = s.query(User).filter(User.username == username).first()
        return token, u.id, u.tenant_id
    finally:
        s.close()


class _FakeCreds:
    token = "fake-access-token"
    refresh_token = "fake-refresh-token"
    token_uri = "https://oauth2.googleapis.com/token"
    scopes = [
        "https://www.googleapis.com/auth/youtube.upload",
        "https://www.googleapis.com/auth/youtube.readonly",
        "https://www.googleapis.com/auth/yt-analytics.readonly",
    ]
    expiry = None


class _FakeFlow:
    def __init__(self, channel_suffix="1"):
        self.credentials = _FakeCreds()

    def authorization_url(self, **kwargs):
        return (f"https://accounts.google.com/o/oauth2/auth?state={kwargs['state']}", kwargs["state"])

    def fetch_token(self, code):
        assert code


def _patch_google(monkeypatch, channel_id="UCabc123", title="Canal Test"):
    import youtube_api

    monkeypatch.setattr(youtube_api, "_make_flow", lambda: _FakeFlow())
    monkeypatch.setattr(
        youtube_api, "_fetch_channel_info",
        lambda creds: {"channel_id": channel_id, "title": title, "thumbnail_url": "https://yt.img/x.jpg"},
    )


def _do_callback(client, monkeypatch, user_id, tenant_id, channel_id="UCabc123", title="Canal Test"):
    import youtube_api

    _patch_google(monkeypatch, channel_id, title)
    state = youtube_api._sign_state(user_id, tenant_id)
    return client.get(
        f"/youtube/oauth/callback?state={state}&code=fakecode",
        follow_redirects=False,
    )


# ─── Connect ─────────────────────────────────────────────────────────

def test_connect_returns_auth_url(client, monkeypatch):
    _patch_google(monkeypatch)
    token, _, _ = _register(client)

    res = client.post("/youtube/channels/connect", headers=auth(token))
    assert res.status_code == 200, res.text
    assert res.json()["auth_url"].startswith("https://accounts.google.com/")


def test_connect_unconfigured_is_503(client, monkeypatch):
    import youtube_api

    def _boom():
        raise youtube_api.YouTubeOAuthNotConfiguredError("no client id")

    monkeypatch.setattr(youtube_api, "_make_flow", _boom)
    token, _, _ = _register(client)

    res = client.post("/youtube/channels/connect", headers=auth(token))
    assert res.status_code == 503


# ─── Callback ────────────────────────────────────────────────────────

def test_callback_tampered_state_redirects_with_error(client):
    res = client.get(
        "/youtube/oauth/callback?state=not-a-jwt&code=x", follow_redirects=False,
    )
    assert res.status_code == 302
    assert "youtube_error=state" in res.headers["location"]


def test_callback_user_denied_redirects_with_error(client):
    res = client.get(
        "/youtube/oauth/callback?error=access_denied", follow_redirects=False,
    )
    assert res.status_code == 302
    assert "youtube_error=access_denied" in res.headers["location"]


def test_callback_missing_scopes_redirects_with_scopes_error(client, monkeypatch):
    """User left a permission checkbox unticked → we must reject the
    connection with a specific 'scopes' error, not store a dead channel."""
    import youtube_api
    from database import SessionLocal, YouTubeChannel

    class _PartialCreds(_FakeCreds):
        # Only "view", missing "upload" — can't publish.
        scopes = ["https://www.googleapis.com/auth/youtube.readonly"]

    class _PartialFlow(_FakeFlow):
        def __init__(self):
            self.credentials = _PartialCreds()

    monkeypatch.setattr(youtube_api, "_make_flow", lambda: _PartialFlow())
    # channel info must NOT be fetched when scopes are insufficient.
    monkeypatch.setattr(youtube_api, "_fetch_channel_info",
                        lambda creds: (_ for _ in ()).throw(AssertionError("should not fetch")))

    _, user_id, tenant_id = _register(client)
    state = youtube_api._sign_state(user_id, tenant_id)
    res = client.get(f"/youtube/oauth/callback?state={state}&code=x", follow_redirects=False)
    assert res.status_code == 302
    assert "youtube_error=scopes" in res.headers["location"]

    # No channel row was created.
    s = SessionLocal()
    try:
        assert s.query(YouTubeChannel).filter(YouTubeChannel.tenant_id == tenant_id).count() == 0
    finally:
        s.close()


def test_callback_happy_path_persists_encrypted_channel(client, monkeypatch):
    from database import SessionLocal, YouTubeChannel, AuditLog
    from token_crypto import decrypt_token

    token, user_id, tenant_id = _register(client)
    res = _do_callback(client, monkeypatch, user_id, tenant_id)
    assert res.status_code == 302
    assert "youtube_connected=1" in res.headers["location"]

    s = SessionLocal()
    try:
        row = (
            s.query(YouTubeChannel)
            .filter(YouTubeChannel.tenant_id == tenant_id)
            .one()
        )
        assert row.channel_id == "UCabc123"
        assert row.channel_title == "Canal Test"
        assert row.is_default is True
        assert row.status == "active"
        # Encrypted at rest, decryptable back to the token dict.
        assert "fake-refresh-token" not in (row.token_encrypted or "")
        assert decrypt_token(row.token_encrypted)["refresh_token"] == "fake-refresh-token"

        entry = (
            s.query(AuditLog)
            .filter(AuditLog.action == "youtube.channel_connected")
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert entry is not None and entry.detail["channel_id"] == "UCabc123"
    finally:
        s.close()

    # Listed via the API, without any token material.
    listed = client.get("/youtube/channels", headers=auth(token)).json()
    assert len(listed) == 1
    assert listed[0]["channel_id"] == "UCabc123"
    assert "token" not in str(listed[0])


def test_reconnect_upserts_same_row(client, monkeypatch):
    from database import SessionLocal, YouTubeChannel

    _, user_id, tenant_id = _register(client)
    _do_callback(client, monkeypatch, user_id, tenant_id)
    _do_callback(client, monkeypatch, user_id, tenant_id, title="Canal Renombrado")

    s = SessionLocal()
    try:
        rows = s.query(YouTubeChannel).filter(YouTubeChannel.tenant_id == tenant_id).all()
        assert len(rows) == 1
        assert rows[0].channel_title == "Canal Renombrado"
    finally:
        s.close()


def test_second_channel_is_not_default(client, monkeypatch):
    _, user_id, tenant_id = _register(client)
    _do_callback(client, monkeypatch, user_id, tenant_id, channel_id="UC-one")
    _do_callback(client, monkeypatch, user_id, tenant_id, channel_id="UC-two")

    from database import SessionLocal, YouTubeChannel
    s = SessionLocal()
    try:
        defaults = {
            r.channel_id: r.is_default
            for r in s.query(YouTubeChannel).filter(YouTubeChannel.tenant_id == tenant_id)
        }
        assert defaults == {"UC-one": True, "UC-two": False}
    finally:
        s.close()


# ─── Tenant isolation ────────────────────────────────────────────────

def test_other_tenant_cannot_see_or_delete_channel(client, monkeypatch):
    token_a, user_a, tenant_a = _register(client)
    token_b, _, _ = _register(client)
    _do_callback(client, monkeypatch, user_a, tenant_a)

    from database import SessionLocal, YouTubeChannel
    s = SessionLocal()
    try:
        pk = s.query(YouTubeChannel.id).filter(YouTubeChannel.tenant_id == tenant_a).scalar()
    finally:
        s.close()

    assert client.get("/youtube/channels", headers=auth(token_b)).json() == []
    assert client.delete(f"/youtube/channels/{pk}", headers=auth(token_b)).status_code == 404
    assert client.post(f"/youtube/channels/{pk}/default", headers=auth(token_b)).status_code == 404


# ─── Disconnect / default ────────────────────────────────────────────

def test_disconnect_revokes_and_promotes_new_default(client, monkeypatch):
    import youtube_api
    from database import SessionLocal, YouTubeChannel

    revoked = {}
    monkeypatch.setattr(
        youtube_api._requests, "post",
        lambda url, params=None, timeout=None: revoked.update({"token": params["token"]}),
    )

    token, user_id, tenant_id = _register(client)
    _do_callback(client, monkeypatch, user_id, tenant_id, channel_id="UC-one")
    _do_callback(client, monkeypatch, user_id, tenant_id, channel_id="UC-two")

    s = SessionLocal()
    try:
        pk_one = (
            s.query(YouTubeChannel.id)
            .filter(YouTubeChannel.tenant_id == tenant_id, YouTubeChannel.channel_id == "UC-one")
            .scalar()
        )
    finally:
        s.close()

    res = client.delete(f"/youtube/channels/{pk_one}", headers=auth(token))
    assert res.status_code == 200
    assert revoked["token"] == "fake-refresh-token"

    s = SessionLocal()
    try:
        rows = {r.channel_id: r for r in s.query(YouTubeChannel).filter(YouTubeChannel.tenant_id == tenant_id)}
        assert rows["UC-one"].status == "revoked"
        assert rows["UC-one"].token_encrypted is None
        assert rows["UC-one"].is_default is False
        # The remaining active channel inherits the default.
        assert rows["UC-two"].is_default is True
    finally:
        s.close()


def test_set_default_switches(client, monkeypatch):
    from database import SessionLocal, YouTubeChannel

    token, user_id, tenant_id = _register(client)
    _do_callback(client, monkeypatch, user_id, tenant_id, channel_id="UC-one")
    _do_callback(client, monkeypatch, user_id, tenant_id, channel_id="UC-two")

    s = SessionLocal()
    try:
        pk_two = (
            s.query(YouTubeChannel.id)
            .filter(YouTubeChannel.tenant_id == tenant_id, YouTubeChannel.channel_id == "UC-two")
            .scalar()
        )
    finally:
        s.close()

    assert client.post(f"/youtube/channels/{pk_two}/default", headers=auth(token)).status_code == 200

    s = SessionLocal()
    try:
        defaults = {
            r.channel_id: r.is_default
            for r in s.query(YouTubeChannel).filter(YouTubeChannel.tenant_id == tenant_id)
        }
        assert defaults == {"UC-one": False, "UC-two": True}
    finally:
        s.close()


# ─── resolve_channel / client construction ───────────────────────────

def test_resolve_channel_prefers_explicit_then_default(client, monkeypatch):
    from database import SessionLocal, YouTubeChannel
    from youtube_upload import resolve_channel, YouTubeNotConfiguredError

    _, user_id, tenant_id = _register(client)
    _do_callback(client, monkeypatch, user_id, tenant_id, channel_id="UC-one")
    _do_callback(client, monkeypatch, user_id, tenant_id, channel_id="UC-two")

    s = SessionLocal()
    try:
        rows = {r.channel_id: r for r in s.query(YouTubeChannel).filter(YouTubeChannel.tenant_id == tenant_id)}
        # Default fallback.
        assert resolve_channel(s, tenant_id).channel_id == "UC-one"
        # Explicit pick.
        assert resolve_channel(s, tenant_id, rows["UC-two"].id).channel_id == "UC-two"
        # Foreign/unknown id.
        with pytest.raises(ValueError):
            resolve_channel(s, tenant_id, 999999)
        # Inactive explicit channel.
        rows["UC-two"].status = "error"
        s.commit()
        with pytest.raises(YouTubeNotConfiguredError):
            resolve_channel(s, tenant_id, rows["UC-two"].id)
        # No channels at all → legacy fallback (None).
        assert resolve_channel(s, "tenant-without-channels") is None
    finally:
        s.close()


def test_credentials_from_disconnected_channel_raises(client, monkeypatch):
    from database import SessionLocal, YouTubeChannel
    from youtube_upload import _credentials_from_channel, YouTubeNotConfiguredError

    _, user_id, tenant_id = _register(client)
    _do_callback(client, monkeypatch, user_id, tenant_id)

    s = SessionLocal()
    try:
        row = s.query(YouTubeChannel).filter(YouTubeChannel.tenant_id == tenant_id).one()
        creds = _credentials_from_channel(row)
        assert creds.refresh_token == "fake-refresh-token"

        row.token_encrypted = None
        with pytest.raises(YouTubeNotConfiguredError):
            _credentials_from_channel(row)
    finally:
        s.close()


def test_persist_refreshed_token_updates_row(client, monkeypatch):
    from database import SessionLocal, YouTubeChannel
    from token_crypto import decrypt_token
    from youtube_upload import _persist_refreshed_token

    _, user_id, tenant_id = _register(client)
    _do_callback(client, monkeypatch, user_id, tenant_id)

    s = SessionLocal()
    try:
        pk = s.query(YouTubeChannel.id).filter(YouTubeChannel.tenant_id == tenant_id).scalar()
    finally:
        s.close()

    _persist_refreshed_token(pk, {"token": "rotated", "refresh_token": "fake-refresh-token"})

    s = SessionLocal()
    try:
        row = s.query(YouTubeChannel).filter(YouTubeChannel.id == pk).one()
        assert decrypt_token(row.token_encrypted)["token"] == "rotated"
        assert row.last_refresh_at is not None
        assert row.last_refresh_error is None
    finally:
        s.close()
