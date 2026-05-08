"""Broadcast (ProRes) feature gating — `has_prores_access` policy and
the /upload endpoint that enforces it.

The product policy: ProRes deliverables are private to allow-listed
tenants and to admins. Self-serve / retail users can't request
`delivery_profile=umg|both` even by hand-crafting the form fields.

Why this matters: UMG is a brand name, broadcast users are a small
B2B audience, and self-serve users seeing those affordances would be
confused at best (and at worst would tax the queue with renders that
will fail the validation on the way out).
"""
import os
import pytest

import auth


def _stub_user(role="user", tenant_id="default"):
    """Lightweight stand-in for the User model — just the attrs the
    helper reads. Avoids needing a DB session for a pure-policy check."""
    class U:
        pass
    u = U()
    u.role = role
    u.tenant_id = tenant_id
    return u


# ─── Pure helper: has_prores_access ──────────────────────────────────

def test_admin_always_has_access(monkeypatch):
    """Admin role bypasses the tenant allow-list — operator can demo
    the feature without opting their admin tenant in."""
    monkeypatch.setattr(auth, "PRORES_TENANTS", set())
    assert auth.has_prores_access(_stub_user(role="admin", tenant_id="default")) is True


def test_regular_user_default_denied(monkeypatch):
    """A self-registered user on the default tenant has no access."""
    monkeypatch.setattr(auth, "PRORES_TENANTS", set())
    assert auth.has_prores_access(_stub_user(role="user", tenant_id="default")) is False


def test_user_in_allowed_tenant_gets_access(monkeypatch):
    """When the operator opts a tenant into PRORES_TENANTS, every
    user in that tenant gains access — no per-user toggle needed."""
    monkeypatch.setattr(auth, "PRORES_TENANTS", {"umg"})
    assert auth.has_prores_access(_stub_user(role="user", tenant_id="umg")) is True


def test_tenant_match_is_case_insensitive(monkeypatch):
    """Operators set env vars in lowercase by convention but tenant_id
    can drift in case — accept either."""
    monkeypatch.setattr(auth, "PRORES_TENANTS", {"umg"})
    assert auth.has_prores_access(_stub_user(role="user", tenant_id="UMG")) is True
    assert auth.has_prores_access(_stub_user(role="user", tenant_id="Umg")) is True


def test_dict_user_shape_supported(monkeypatch):
    """`get_current_user` returns a dict, not the model — helper has
    to handle both shapes so we don't have to convert at every gate."""
    monkeypatch.setattr(auth, "PRORES_TENANTS", {"umg"})
    assert auth.has_prores_access({"role": "user", "tenant_id": "umg"}) is True
    assert auth.has_prores_access({"role": "user", "tenant_id": "default"}) is False
    assert auth.has_prores_access({"role": "admin", "tenant_id": "anything"}) is True


def test_none_user_denied():
    """Unauthenticated callers (None) never get access."""
    assert auth.has_prores_access(None) is False


# ─── End-to-end: /upload form submission gated by the helper ────────

def _make_audio_payload(name="test.mp3"):
    """Tiny ID3-tagged MP3-ish blob — passes the fastapi UploadFile
    sniffing without needing a real audio file. /upload's validator
    only checks magic bytes + extension."""
    # ID3v2 header + minimal MP3 frame
    return name, b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" * 64, "audio/mpeg"


def test_regular_user_cannot_request_broadcast_delivery(monkeypatch, client, user_token):
    """A self-serve user posting delivery_profile=both gets 403 —
    even if they craft the form fields by hand."""
    monkeypatch.setattr(auth, "PRORES_TENANTS", set())
    name, content, ctype = _make_audio_payload()
    res = client.post(
        "/upload",
        headers={"Authorization": f"Bearer {user_token}"},
        files={"file": (name, content, ctype)},
        data={
            "artist": "Test Artist",
            "delivery_profile": "both",
            "umg_frame_size": "HD",
            "umg_fps": "24",
            "umg_prores_profile": "3",
        },
    )
    assert res.status_code == 403, f"expected 403, got {res.status_code}: {res.text[:200]}"
    assert "ProRes" in res.json().get("detail", "") or "Broadcast" in res.json().get("detail", "")


def test_admin_can_request_broadcast_delivery(monkeypatch, client, admin_token):
    """Admin gets through the gate — they may still hit downstream
    validation (real audio, quota, etc.) but NOT the 403."""
    monkeypatch.setattr(auth, "PRORES_TENANTS", set())
    name, content, ctype = _make_audio_payload()
    res = client.post(
        "/upload",
        headers={"Authorization": f"Bearer {admin_token}"},
        files={"file": (name, content, ctype)},
        data={
            "artist": "Test Artist",
            "delivery_profile": "both",
            "umg_frame_size": "HD",
            "umg_fps": "24",
            "umg_prores_profile": "3",
        },
    )
    # Could be 200 (queued), 400 (validation), 429 (cap), 503 (disk) —
    # but NOT 403. The single explicit assertion is "didn't hit RBAC".
    assert res.status_code != 403, f"admin got 403: {res.text[:200]}"


def test_regular_user_can_request_youtube_delivery(client, user_token):
    """delivery_profile=youtube is the default product and unaffected
    by the ProRes gate. The endpoint may still 4xx for unrelated
    reasons (AI authorization, plan caps, audio validation) — assert
    only that the failure (if any) is NOT the ProRes/Broadcast gate."""
    name, content, ctype = _make_audio_payload()
    res = client.post(
        "/upload",
        headers={"Authorization": f"Bearer {user_token}"},
        files={"file": (name, content, ctype)},
        data={"artist": "Test Artist", "delivery_profile": "youtube"},
    )
    if res.status_code == 403:
        detail = res.json().get("detail", "")
        assert "ProRes" not in detail and "Broadcast" not in detail, (
            f"YouTube request hit ProRes gate (regression): {detail}"
        )


# ─── /me / /auth/login response shape ────────────────────────────────

def test_me_response_includes_features_dict(monkeypatch, client, user_token):
    """Frontend gates UI on `user.features.prores_export` — verify
    that field is present in the response shape, with the right
    value for a non-eligible user."""
    monkeypatch.setattr(auth, "PRORES_TENANTS", set())
    res = client.get("/auth/me", headers={"Authorization": f"Bearer {user_token}"})
    assert res.status_code == 200
    body = res.json()
    assert "features" in body, f"missing 'features' in /me: {body}"
    assert body["features"].get("prores_export") is False


def test_login_response_includes_features_dict(client, db):
    """The /auth/login response is what the frontend caches; needs
    the features dict so the UI can gate on first paint without
    waiting for /me."""
    from auth import create_user
    create_user(
        db,
        username="rbac_test_user",
        password="testpass12345",
        email="rbac@test.com",
    )
    res = client.post("/auth/login", json={
        "username": "rbac_test_user", "password": "testpass12345",
    })
    assert res.status_code == 200
    body = res.json()
    assert "user" in body and "features" in body["user"]
    assert "prores_export" in body["user"]["features"]
