"""Tests for background library and compliance features."""

import io
import os
from tests.conftest import auth


# ---------------------------------------------------------------------------
# Background Library CRUD
# ---------------------------------------------------------------------------

def test_list_backgrounds_empty(client, admin_token):
    """List backgrounds returns empty list initially."""
    res = client.get("/backgrounds", headers=auth(admin_token))
    assert res.status_code == 200
    assert res.json() == []


def test_admin_upload_background(client, admin_token):
    """Admin can upload a background asset."""
    # Create a small fake MP4 file (just bytes, not a real video)
    fake_video = io.BytesIO(b"\x00" * 1024)
    res = client.post(
        "/admin/backgrounds",
        headers=auth(admin_token),
        files={"file": ("test_bg.mp4", fake_video, "video/mp4")},
        data={"name": "Ocean Sunset", "tags": "ocean,sunset,calm"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["name"] == "Ocean Sunset"
    assert data["tags"] == ["ocean", "sunset", "calm"]
    assert data["file_type"] == "mp4"
    assert data["id"] > 0
    return data["id"]


def test_admin_upload_background_jpg(client, admin_token):
    """Admin can upload a JPG background."""
    fake_img = io.BytesIO(b"\xff\xd8\xff" + b"\x00" * 512)
    res = client.post(
        "/admin/backgrounds",
        headers=auth(admin_token),
        files={"file": ("sunset.jpg", fake_img, "image/jpeg")},
        data={"name": "Sunset Still", "tags": "sunset"},
    )
    assert res.status_code == 200
    assert res.json()["file_type"] == "jpg"


def test_admin_upload_background_invalid_type(client, admin_token):
    """Reject non-video/image files."""
    fake_file = io.BytesIO(b"not a video")
    res = client.post(
        "/admin/backgrounds",
        headers=auth(admin_token),
        files={"file": ("test.txt", fake_file, "text/plain")},
        data={"name": "Bad File", "tags": ""},
    )
    assert res.status_code == 400


def test_list_backgrounds_after_upload(client, admin_token):
    """List shows uploaded backgrounds."""
    res = client.get("/admin/backgrounds", headers=auth(admin_token))
    assert res.status_code == 200
    bgs = res.json()
    assert len(bgs) >= 1
    names = [b["name"] for b in bgs]
    assert "Ocean Sunset" in names


def test_user_list_backgrounds(client, user_token):
    """Regular users can list available backgrounds."""
    res = client.get("/backgrounds", headers=auth(user_token))
    assert res.status_code == 200
    assert isinstance(res.json(), list)


def test_preview_background(client, admin_token):
    """Preview endpoint serves the file."""
    # Get the first background
    bgs = client.get("/admin/backgrounds", headers=auth(admin_token)).json()
    assert len(bgs) > 0
    bg_id = bgs[0]["id"]

    token = admin_token
    res = client.get(f"/backgrounds/{bg_id}/preview?token={token}")
    assert res.status_code == 200


def test_preview_background_not_found(client, admin_token):
    """Preview returns 404 for non-existent asset."""
    res = client.get(f"/backgrounds/99999/preview?token={admin_token}")
    assert res.status_code == 404


def test_delete_background(client, admin_token):
    """Admin can delete a background."""
    # Upload one to delete
    fake = io.BytesIO(b"\x00" * 256)
    upload_res = client.post(
        "/admin/backgrounds",
        headers=auth(admin_token),
        files={"file": ("delete_me.mp4", fake, "video/mp4")},
        data={"name": "To Delete", "tags": ""},
    )
    bg_id = upload_res.json()["id"]

    # Delete it
    res = client.delete(f"/admin/backgrounds/{bg_id}", headers=auth(admin_token))
    assert res.status_code == 200
    assert res.json()["ok"] is True

    # Verify it's gone
    bgs = client.get("/admin/backgrounds", headers=auth(admin_token)).json()
    assert bg_id not in [b["id"] for b in bgs]


def test_user_cannot_upload_background(client, user_token):
    """Regular users cannot upload backgrounds (admin only)."""
    fake = io.BytesIO(b"\x00" * 256)
    res = client.post(
        "/admin/backgrounds",
        headers=auth(user_token),
        files={"file": ("hack.mp4", fake, "video/mp4")},
        data={"name": "Unauthorized", "tags": ""},
    )
    assert res.status_code == 403


def test_user_cannot_delete_background(client, user_token):
    """Regular users cannot delete backgrounds."""
    res = client.delete("/admin/backgrounds/1", headers=auth(user_token))
    assert res.status_code == 403


# ---------------------------------------------------------------------------
# Compliance endpoints
# ---------------------------------------------------------------------------

def test_compliance_status(client, admin_token):
    """Compliance status endpoint returns all checks."""
    res = client.get("/compliance/status", headers=auth(admin_token))
    assert res.status_code == 200
    data = res.json()
    assert "checks" in data
    assert "guideline_1_tools" in data["checks"]
    assert "guideline_3_prohibited_tools" in data["checks"]
    assert "guideline_17_provenance" in data["checks"]
    # Verify each check has status and detail
    for key, check in data["checks"].items():
        assert "status" in check, f"{key} missing status"
        assert "detail" in check, f"{key} missing detail"


def test_compliance_data_policy(client, admin_token):
    """Data policy endpoint returns full policy."""
    res = client.get("/compliance/data-policy", headers=auth(admin_token))
    assert res.status_code == 200
    data = res.json()
    assert data["platform"] == "GenLy AI"
    assert "training_policy" in data
    assert data["training_policy"]["fine_tuning"] == "GenLy AI does not perform fine-tuning on any models."
    assert "data_sent_to_ai" in data
    assert len(data["data_sent_to_ai"]) >= 4


# ---------------------------------------------------------------------------
# Approval workflow
# ---------------------------------------------------------------------------

def test_approve_nonexistent_job(client, admin_token):
    """Approve returns 404 for non-existent job."""
    res = client.post(
        "/approve/nonexistent123",
        headers={**auth(admin_token), "Content-Type": "application/json"},
        json={"notes": ""},
    )
    assert res.status_code == 404


def test_reject_nonexistent_job(client, admin_token):
    """Reject returns 404 for non-existent job."""
    res = client.post(
        "/reject/nonexistent123",
        headers={**auth(admin_token), "Content-Type": "application/json"},
        json={"notes": ""},
    )
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# Provenance endpoints
# ---------------------------------------------------------------------------

def test_provenance_nonexistent_job(client, admin_token):
    """Provenance returns 404 for non-existent job."""
    res = client.get("/provenance/nonexistent123", headers=auth(admin_token))
    assert res.status_code == 404


def test_provenance_export_nonexistent_job(client, admin_token):
    """Provenance export returns 404 for non-existent job."""
    res = client.get("/provenance/nonexistent123/export", headers=auth(admin_token))
    assert res.status_code == 404


def test_admin_provenance_list(client, admin_token):
    """Admin can list all provenance records."""
    res = client.get("/admin/provenance", headers=auth(admin_token))
    assert res.status_code == 200
    data = res.json()
    assert "total" in data
    assert "records" in data


# ---------------------------------------------------------------------------
# AI Authorization
# ---------------------------------------------------------------------------

def test_admin_authorize_user(client, admin_token, user_token):
    """Admin can authorize a user for AI."""
    # Get user info
    me = client.get("/auth/me", headers=auth(user_token)).json()
    user_id = me["id"]

    # Authorize
    res = client.post(f"/admin/users/{user_id}/authorize-ai", headers=auth(admin_token))
    assert res.status_code == 200
    assert res.json()["ai_authorized"] is True

    # Verify
    user_detail = client.get(f"/admin/users/{user_id}", headers=auth(admin_token)).json()
    assert user_detail["ai_authorized"] is True


def test_admin_revoke_user(client, admin_token, user_token):
    """Admin can revoke AI authorization."""
    me = client.get("/auth/me", headers=auth(user_token)).json()
    user_id = me["id"]

    # First authorize
    client.post(f"/admin/users/{user_id}/authorize-ai", headers=auth(admin_token))

    # Then revoke
    res = client.post(f"/admin/users/{user_id}/revoke-ai", headers=auth(admin_token))
    assert res.status_code == 200
    assert res.json()["ai_authorized"] is False


def test_unauthorized_user_blocked_from_upload(client, user_token):
    """Non-authorized user gets 403 on upload."""
    fake_mp3 = io.BytesIO(b"ID3" + b"\x00" * 253)
    res = client.post(
        "/upload",
        headers=auth(user_token),
        files={"file": ("test.mp3", fake_mp3, "audio/mpeg")},
        data={"artist": "Test Artist", "style": "oscuro"},
    )
    # Should be 403 because user is not ai_authorized
    assert res.status_code == 403
    assert "not authorized" in res.json()["detail"].lower()


def test_authorized_user_can_upload(client, admin_token, user_token):
    """Authorized user is NOT blocked by AI auth check."""
    me = client.get("/auth/me", headers=auth(user_token)).json()
    user_id = me["id"]

    # Authorize the user
    client.post(f"/admin/users/{user_id}/authorize-ai", headers=auth(admin_token))

    # Upload should pass the auth check (may fail later on pipeline, but not 403)
    fake_mp3 = io.BytesIO(b"ID3" + b"\x00" * 253)
    res = client.post(
        "/upload",
        headers=auth(user_token),
        files={"file": ("test.mp3", fake_mp3, "audio/mpeg")},
        data={"artist": "Test Artist", "style": "oscuro"},
    )
    assert res.status_code != 403, f"Authorized user should not get 403, got {res.status_code}"


def test_revoked_user_blocked_again(client, admin_token, user_token):
    """User authorized then revoked is blocked again."""
    me = client.get("/auth/me", headers=auth(user_token)).json()
    user_id = me["id"]

    # Authorize then revoke
    client.post(f"/admin/users/{user_id}/authorize-ai", headers=auth(admin_token))
    client.post(f"/admin/users/{user_id}/revoke-ai", headers=auth(admin_token))

    # Should be blocked
    fake_mp3 = io.BytesIO(b"ID3" + b"\x00" * 253)
    res = client.post(
        "/upload",
        headers=auth(user_token),
        files={"file": ("test.mp3", fake_mp3, "audio/mpeg")},
        data={"artist": "Test Artist", "style": "oscuro"},
    )
    assert res.status_code == 403


def test_admin_always_passes_ai_auth(client, admin_token):
    """Admins are always allowed regardless of ai_authorized flag."""
    fake_mp3 = io.BytesIO(b"ID3" + b"\x00" * 253)
    res = client.post(
        "/upload",
        headers=auth(admin_token),
        files={"file": ("test.mp3", fake_mp3, "audio/mpeg")},
        data={"artist": "Test Artist", "style": "oscuro"},
    )
    # Admin should NOT get 403 (may get other errors from pipeline but not auth)
    assert res.status_code != 403, f"Admin should not get 403, got {res.status_code}"


def test_unauthorized_user_blocked_from_generate(client, user_token):
    """Non-authorized user gets 403 on /generate too."""
    fake_mp3 = io.BytesIO(b"ID3" + b"\x00" * 253)
    res = client.post(
        "/generate",
        headers=auth(user_token),
        files={"file": ("test.mp3", fake_mp3, "audio/mpeg")},
        data={"artist": "Test", "style": "oscuro", "segments_json": "[]"},
    )
    assert res.status_code == 403


def test_library_background_bypasses_ai_auth(client, admin_token, user_token):
    """Using a library background skips AI auth check (no AI generation needed)."""
    # Upload a background as admin
    fake_bg = io.BytesIO(b"\x00" * 512)
    bg_res = client.post(
        "/admin/backgrounds",
        headers=auth(admin_token),
        files={"file": ("lib_bg.mp4", fake_bg, "video/mp4")},
        data={"name": "Library Test", "tags": "test"},
    )
    bg_id = bg_res.json()["id"]

    # User is NOT ai_authorized, but using library background should bypass
    fake_mp3 = io.BytesIO(b"ID3" + b"\x00" * 253)
    res = client.post(
        "/upload",
        headers=auth(user_token),
        files={"file": ("test.mp3", fake_mp3, "audio/mpeg")},
        data={"artist": "Test Artist", "style": "oscuro", "background_id": str(bg_id)},
    )
    # Should NOT be 403 — library backgrounds bypass AI auth
    assert res.status_code != 403, f"Library bg should bypass AI auth, got {res.status_code}"
