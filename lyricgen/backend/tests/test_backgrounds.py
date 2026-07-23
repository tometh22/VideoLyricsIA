"""Tests for background library and compliance features."""

import io
import os
import subprocess
import pytest
from tests.conftest import auth


@pytest.fixture(scope="module")
def valid_mp4_bytes(tmp_path_factory):
    """Small real MP4: admin validation deliberately uses ffprobe."""
    output = tmp_path_factory.mktemp("background-fixtures") / "valid.mp4"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", "color=c=blue:s=16x16:d=0.2",
            "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            str(output),
        ],
        check=True,
        timeout=20,
    )
    return output.read_bytes()


@pytest.fixture(scope="module")
def valid_jpeg_bytes():
    from PIL import Image

    output = io.BytesIO()
    Image.new("RGB", (16, 16), color="blue").save(output, format="JPEG")
    return output.getvalue()


# ---------------------------------------------------------------------------
# Background Library CRUD
# ---------------------------------------------------------------------------

def test_list_backgrounds_empty(client, admin_token):
    """List backgrounds returns empty list initially."""
    res = client.get("/backgrounds", headers=auth(admin_token))
    assert res.status_code == 200
    assert res.json() == []


def test_admin_upload_background(client, admin_token, valid_mp4_bytes):
    """Admin can upload a background asset."""
    # Minimal ISO-BMFF header accepted by the upload integrity gate.
    fake_video = io.BytesIO(valid_mp4_bytes)
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


def test_admin_upload_background_jpg(client, admin_token, valid_jpeg_bytes):
    """Admin can upload a JPG background."""
    fake_img = io.BytesIO(valid_jpeg_bytes)
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


def test_admin_upload_background_rejects_corrupt_mp4(client, admin_token):
    """An extension alone must never make malformed media selectable."""
    res = client.post(
        "/admin/backgrounds",
        headers=auth(admin_token),
        files={"file": ("corrupt.mp4", io.BytesIO(b"\x00" * 1024), "video/mp4")},
        data={"name": "Corrupt", "tags": ""},
    )
    assert res.status_code == 400
    assert "magic bytes" in res.json()["detail"]


def test_admin_upload_background_rejects_truncated_mp4(client, admin_token):
    """A plausible ftyp header without decodable media is still invalid."""
    truncated = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 128
    res = client.post(
        "/admin/backgrounds",
        headers=auth(admin_token),
        files={"file": ("truncated.mp4", io.BytesIO(truncated), "video/mp4")},
        data={"name": "Truncated", "tags": ""},
    )
    assert res.status_code == 400
    assert "decoded" in res.json()["detail"]


def test_admin_upload_background_fails_closed_when_r2_upload_fails(
    client, admin_token, monkeypatch, valid_mp4_bytes,
):
    """Configured object storage cannot degrade to one replica's local disk."""
    import storage

    monkeypatch.setattr(storage, "is_enabled", lambda: True)
    monkeypatch.setattr(
        storage,
        "upload_file",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("r2 down")),
    )
    res = client.post(
        "/admin/backgrounds",
        headers=auth(admin_token),
        files={"file": ("valid.mp4", io.BytesIO(valid_mp4_bytes), "video/mp4")},
        data={"name": "Must not persist", "tags": ""},
    )
    assert res.status_code == 503
    assert "storage" in res.json()["detail"].lower()


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

    issued = client.post(
        "/backgrounds/preview-tokens",
        json={"asset_ids": [bg_id]},
        headers=auth(admin_token),
    )
    assert issued.status_code == 200
    token = issued.json()["tokens"][str(bg_id)]
    res = client.get(f"/backgrounds/{bg_id}/preview?token={token}")
    assert res.status_code == 200

    # A full access credential is never accepted by a URL preview route.
    rejected = client.get(f"/backgrounds/{bg_id}/preview?token={admin_token}")
    assert rejected.status_code == 401


def test_preview_background_not_found(client, admin_token):
    """Preview returns 404 for non-existent asset."""
    from auth import create_media_token
    from database import SessionLocal, User
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == "admin").first()
        media_token = create_media_token(user, "background:99999", "preview")
    finally:
        db.close()
    res = client.get(f"/backgrounds/99999/preview?token={media_token}")
    assert res.status_code == 404


def test_r2_background_preview_url_matches_scoped_token_ttl(
    client, admin_token, db, monkeypatch,
):
    from auth import MEDIA_TOKEN_EXPIRE_SECONDS
    from database import BackgroundAsset
    import storage

    asset = BackgroundAsset(
        name="R2 preview TTL",
        filename="library/global/preview-ttl.jpg",
        file_type="jpg",
        is_active=True,
    )
    db.add(asset)
    db.commit()
    issued = client.post(
        "/backgrounds/preview-tokens",
        json={"asset_ids": [asset.id]},
        headers=auth(admin_token),
    )
    token = issued.json()["tokens"][str(asset.id)]
    calls = []
    monkeypatch.setattr(storage, "is_enabled", lambda: True)
    monkeypatch.setattr(
        storage,
        "generate_signed_url",
        lambda key, expiry_seconds=900: calls.append(
            (key, expiry_seconds)
        ) or f"https://r2.fake/{key}",
    )

    response = client.get(
        f"/backgrounds/{asset.id}/preview?token={token}",
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert calls == [(asset.filename, MEDIA_TOKEN_EXPIRE_SECONDS)]


def test_inactive_background_is_hidden_from_regular_preview(
    client, admin_token, user_token, valid_jpeg_bytes,
):
    from auth import create_media_token
    from database import BackgroundAsset, SessionLocal, User

    uploaded = client.post(
        "/admin/backgrounds",
        headers=auth(admin_token),
        files={"file": ("inactive.jpg", io.BytesIO(valid_jpeg_bytes), "image/jpeg")},
        data={"name": "Inactive", "tags": ""},
    )
    assert uploaded.status_code == 200
    asset_id = uploaded.json()["id"]
    user_id = client.get("/auth/me", headers=auth(user_token)).json()["id"]
    db = SessionLocal()
    try:
        asset = db.query(BackgroundAsset).filter(BackgroundAsset.id == asset_id).first()
        asset.is_active = False
        user = db.query(User).filter(User.id == user_id).first()
        media_token = create_media_token(user, f"background:{asset_id}", "preview")
        db.commit()
    finally:
        db.close()

    issued = client.post(
        "/backgrounds/preview-tokens",
        json={"asset_ids": [asset_id]}, headers=auth(user_token),
    )
    assert issued.status_code == 200
    assert str(asset_id) not in issued.json()["tokens"]
    assert client.get(
        f"/backgrounds/{asset_id}/preview?token={media_token}",
    ).status_code == 404

    # Moderation remains explicit: admins may inspect an inactive asset.
    admin_issued = client.post(
        "/backgrounds/preview-tokens",
        json={"asset_ids": [asset_id]}, headers=auth(admin_token),
    )
    assert str(asset_id) in admin_issued.json()["tokens"]


def test_delete_background(client, admin_token, valid_mp4_bytes):
    """Admin can delete a background."""
    # Upload one to delete
    fake = io.BytesIO(valid_mp4_bytes)
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


def test_unauthorized_user_blocked_from_upload(client, unauthorized_user_token):
    """Non-authorized user gets 403 on upload."""
    fake_mp3 = io.BytesIO(b"ID3" + b"\x00" * 253)
    res = client.post(
        "/upload",
        headers=auth(unauthorized_user_token),
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


def test_unauthorized_user_blocked_from_generate(client, unauthorized_user_token):
    """Non-authorized user gets 403 on /generate too."""
    fake_mp3 = io.BytesIO(b"ID3" + b"\x00" * 253)
    res = client.post(
        "/generate",
        headers=auth(unauthorized_user_token),
        files={"file": ("test.mp3", fake_mp3, "audio/mpeg")},
        data={"artist": "Test", "style": "oscuro", "segments_json": "[]"},
    )
    assert res.status_code == 403


def test_library_background_bypasses_ai_auth(
    client, admin_token, unauthorized_user_token, valid_mp4_bytes,
):
    """Using a library background skips AI auth check (no AI generation needed)."""
    # Upload a background as admin
    fake_bg = io.BytesIO(valid_mp4_bytes)
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
        headers=auth(unauthorized_user_token),
        files={"file": ("test.mp3", fake_mp3, "audio/mpeg")},
        data={"artist": "Test Artist", "style": "oscuro", "background_id": str(bg_id)},
    )
    # Should NOT be 403 — library backgrounds bypass AI auth
    assert res.status_code != 403, f"Library bg should bypass AI auth, got {res.status_code}"
