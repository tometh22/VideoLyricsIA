"""Tests del flujo de delivery R2 → Drive (PR-D2).

- /jobs/{id}/deliver-to-drive con/sin Drive conectado, varios edge cases
- /drive/transfers/{id} status reporting
- ensure_genly_folder con mocks de Drive API
- _build_rclone_config formato correcto
- _build_drive_filename pattern
- run_drive_delivery happy path + error path (con mock de upload_via_rclone)

NO ejecutamos rclone real ni pegamos a Drive API. Todo mockeado.
"""
import os
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from cryptography.fernet import Fernet

# Defaults para que drive_oauth importe limpio
os.environ.setdefault("DRIVE_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
os.environ.setdefault("GOOGLE_OAUTH_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("GOOGLE_OAUTH_REDIRECT_URI", "http://localhost:8000/drive/callback")

from database import Job as JobModel, DriveTransfer, UserDriveTokens  # noqa: E402
from drive_oauth import encrypt_token  # noqa: E402
from drive_uploader import (  # noqa: E402
    _build_drive_filename,
    _build_rclone_config,
    FILE_TYPE_TO_DRIVE_NAME,
    DriveUploadError,
    ensure_genly_folder,
    GDRIVE_FOLDER_NAME,
)


# ─── _build_drive_filename ─────────────────────────────────────────

def test_drive_filename_includes_job_id():
    fn = _build_drive_filename("abc123def456", "umg_master")
    assert fn == "abc123def456__umg_master.mov"


def test_drive_filename_for_each_file_type():
    for file_type, expected_base in FILE_TYPE_TO_DRIVE_NAME.items():
        fn = _build_drive_filename("jobXYZ", file_type)
        assert fn == f"jobXYZ__{expected_base}"


# ─── _build_rclone_config ──────────────────────────────────────────

def test_rclone_config_has_both_remotes():
    cfg = _build_rclone_config(
        r2_access_key="r2-ak",
        r2_secret_key="r2-sk",
        r2_endpoint="https://example.r2.cloudflarestorage.com",
        drive_client_id="drive-cid",
        drive_client_secret="drive-cs",
        drive_access_token="ya29.access",
        drive_refresh_token="1//refresh",
    )
    assert "[r2]" in cfg
    assert "[gdrive]" in cfg
    assert "type = s3" in cfg
    assert "provider = Cloudflare" in cfg
    assert "type = drive" in cfg
    assert "r2-ak" in cfg
    assert "drive-cid" in cfg
    # Token formato JSON serializado (rclone lo espera así)
    assert '"access_token": "ya29.access"' in cfg
    assert '"refresh_token": "1//refresh"' in cfg


# ─── ensure_genly_folder ───────────────────────────────────────────

def test_ensure_genly_folder_returns_existing_id():
    """Si la carpeta ya existe en Drive, devolvemos su ID sin crear nueva."""
    mock_response = MagicMock()
    mock_response.ok = True
    mock_response.json.return_value = {
        "files": [{"id": "folder123", "name": GDRIVE_FOLDER_NAME}]
    }
    with patch("drive_uploader.requests.get", return_value=mock_response):
        folder_id = ensure_genly_folder("ya29.fake-access-token")
    assert folder_id == "folder123"


def test_ensure_genly_folder_creates_if_missing():
    """Carpeta no existe → POST crea, devuelve nuevo ID."""
    list_resp = MagicMock()
    list_resp.ok = True
    list_resp.json.return_value = {"files": []}

    create_resp = MagicMock()
    create_resp.ok = True
    create_resp.json.return_value = {"id": "new-folder-id", "name": GDRIVE_FOLDER_NAME}

    with patch("drive_uploader.requests.get", return_value=list_resp), \
         patch("drive_uploader.requests.post", return_value=create_resp):
        folder_id = ensure_genly_folder("ya29.fake")
    assert folder_id == "new-folder-id"


def test_ensure_genly_folder_raises_on_search_failure():
    fail_resp = MagicMock()
    fail_resp.ok = False
    fail_resp.status_code = 401
    fail_resp.text = "Invalid Credentials"
    with patch("drive_uploader.requests.get", return_value=fail_resp):
        with pytest.raises(DriveUploadError):
            ensure_genly_folder("expired-token")


# ─── /jobs/{id}/deliver-to-drive endpoint ──────────────────────────

def _create_done_job(db, tenant_id="default", umg_spec=None) -> str:
    job_id = uuid.uuid4().hex[:12]
    job = JobModel(
        job_id=job_id,
        user_id=1,
        tenant_id=tenant_id,
        artist="Test",
        song_title="Drive Test",
        filename="test.mp3",
        status="done",
        delivery_profile="youtube" if umg_spec is None else "umg",
        umg_spec=umg_spec,
        progress=100,
    )
    db.add(job)
    db.commit()
    return job_id


def test_deliver_requires_drive_connected(client, admin_token, db):
    """Sin user_drive_tokens row, endpoint devuelve 412."""
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {admin_token}"}).json()
    db.query(UserDriveTokens).filter(UserDriveTokens.user_id == me["id"]).delete()
    db.commit()
    job_id = _create_done_job(db)
    res = client.post(
        f"/jobs/{job_id}/deliver-to-drive",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"file_type": "video"},
    )
    assert res.status_code == 412
    assert "Drive" in res.json()["detail"]


def _connect_drive_for(db, client, token: str):
    """Helper: pone una row user_drive_tokens para el user actual.
    Borra cualquier row previa para ese user (test isolation: la columna
    user_id tiene UNIQUE constraint y los tests comparten la misma DB)."""
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).json()
    db.query(UserDriveTokens).filter(UserDriveTokens.user_id == me["id"]).delete()
    db.flush()
    db.add(UserDriveTokens(
        user_id=me["id"],
        encrypted_refresh_token=encrypt_token("1//fake-refresh"),
        scope="https://www.googleapis.com/auth/drive.file",
        google_email="user@test.com",
    ))
    db.commit()
    return me["id"]


def test_deliver_rejects_unknown_file_type(client, admin_token, db):
    job_id = _create_done_job(db)
    _connect_drive_for(db, client, admin_token)
    res = client.post(
        f"/jobs/{job_id}/deliver-to-drive",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"file_type": "ninguno"},
    )
    assert res.status_code == 400


def test_deliver_rejects_non_done_job(client, admin_token, db):
    """Job en processing/error/etc no puede exportarse."""
    job_id = uuid.uuid4().hex[:12]
    job = JobModel(
        job_id=job_id, user_id=1, tenant_id="default", artist="A", song_title="x",
        filename="test.mp3", status="processing", delivery_profile="youtube",
    )
    db.add(job)
    db.commit()
    _connect_drive_for(db, client, admin_token)
    res = client.post(
        f"/jobs/{job_id}/deliver-to-drive",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"file_type": "video"},
    )
    assert res.status_code == 400


def test_deliver_rejects_umg_master_without_umg_spec(client, admin_token, db):
    """umg_master requiere umg_spec persistido (sino el .mov no existe)."""
    job_id = _create_done_job(db, umg_spec=None)
    _connect_drive_for(db, client, admin_token)
    res = client.post(
        f"/jobs/{job_id}/deliver-to-drive",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"file_type": "umg_master"},
    )
    assert res.status_code == 400
    assert "ProRes" in res.json()["detail"]


def test_deliver_happy_path_creates_transfer_row(client, admin_token, db):
    job_id = _create_done_job(db)
    _connect_drive_for(db, client, admin_token)

    # Mock enqueue para no necesitar Redis
    with patch("main.enqueue_drive_delivery", return_value="drive:fake-job-id"):
        res = client.post(
            f"/jobs/{job_id}/deliver-to-drive",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"file_type": "video"},
        )

    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert "transfer_id" in body
    assert body["status"] == "queued"

    db.expire_all()
    row = db.query(DriveTransfer).filter(DriveTransfer.id == body["transfer_id"]).first()
    assert row is not None
    assert row.status == "queued"
    assert row.file_type == "video"
    assert row.job_id == job_id


def test_deliver_enqueue_failure_marks_transfer_error(client, admin_token, db):
    """Si Redis está down, la row queda en error con mensaje visible."""
    job_id = _create_done_job(db)
    _connect_drive_for(db, client, admin_token)

    with patch(
        "main.enqueue_drive_delivery",
        side_effect=RuntimeError("Redis unreachable"),
    ):
        res = client.post(
            f"/jobs/{job_id}/deliver-to-drive",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"file_type": "video"},
        )
    assert res.status_code == 503

    # La row de DriveTransfer fue creada con status=error
    db.expire_all()
    rows = db.query(DriveTransfer).filter(DriveTransfer.job_id == job_id).all()
    assert len(rows) == 1
    assert rows[0].status == "error"
    assert "Redis" in (rows[0].error or "")


# ─── /drive/transfers/{id} endpoint ────────────────────────────────

def test_get_transfer_returns_404_when_missing(client, admin_token):
    res = client.get(
        "/drive/transfers/nonexistent",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 404


def test_get_transfer_returns_404_for_other_user(client, admin_token, user_token, db, monkeypatch):
    """Tenant isolation: user_token no puede ver transfer de admin.
    Habilitamos drive para el user para que pase el has_drive_access guard
    y lleguemos al check de tenant isolation (user_id filter → 404)."""
    import auth as auth_module
    me_admin = client.get("/auth/me", headers={"Authorization": f"Bearer {admin_token}"}).json()
    me_user = client.get("/auth/me", headers={"Authorization": f"Bearer {user_token}"}).json()
    # Habilitar drive para el tenant del user para pasar el primer guard
    monkeypatch.setattr(auth_module, "DRIVE_ENABLED_TENANTS", {me_user["tenant_id"].lower()})

    job_id = _create_done_job(db)
    transfer_id = f"iso_{me_admin['id']}_12"
    db.query(DriveTransfer).filter(DriveTransfer.id == transfer_id).delete()
    db.flush()
    transfer = DriveTransfer(
        id=transfer_id,
        user_id=me_admin["id"],
        job_id=job_id,
        file_type="video",
        status="queued",
    )
    db.add(transfer)
    db.commit()

    res = client.get(
        f"/drive/transfers/{transfer.id}",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    # Transfer pertenece al admin — user con drive habilitado recibe 404
    assert res.status_code == 404


def test_get_transfer_returns_full_status(client, admin_token, db):
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {admin_token}"}).json()
    job_id = _create_done_job(db)
    transfer = DriveTransfer(
        id="status-test1",
        user_id=me["id"],
        job_id=job_id,
        file_type="umg_master",
        status="running",
        progress_pct=42,
        bytes_transferred=1_073_741_824,
        bytes_total=2_147_483_648,
    )
    db.add(transfer)
    db.commit()

    res = client.get(
        f"/drive/transfers/{transfer.id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["id"] == "status-test1"
    assert body["status"] == "running"
    assert body["progress_pct"] == 42
    assert body["bytes_transferred"] == 1_073_741_824
    assert body["bytes_total"] == 2_147_483_648
    assert body["file_type"] == "umg_master"


def test_get_transfer_includes_drive_link_on_done(client, admin_token, db):
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {admin_token}"}).json()
    job_id = _create_done_job(db)
    transfer = DriveTransfer(
        id="done-test12",
        user_id=me["id"],
        job_id=job_id,
        file_type="video",
        status="done",
        progress_pct=100,
        drive_file_id="drive-file-abc",
        web_view_link="https://drive.google.com/file/d/drive-file-abc/view",
        completed_at=datetime.now(timezone.utc),
    )
    db.add(transfer)
    db.commit()

    res = client.get(
        f"/drive/transfers/{transfer.id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "done"
    assert body["drive_file_id"] == "drive-file-abc"
    assert body["web_view_link"] == "https://drive.google.com/file/d/drive-file-abc/view"
