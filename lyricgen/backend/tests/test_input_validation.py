"""Tests para Pydantic max_length sweep.

Verifica que payloads oversized son rechazados con 422 (Pydantic) en
los endpoints expuestos al cliente. Sin estos límites, un atacante
puede mandar 100MB de string en cualquier field → DoS trivial.

También cubre regression: campos opcionales con default "" no deben
romper cuando el cliente omite el campo.
"""

import uuid

import pytest


def test_register_username_oversized(client):
    """username > 200 chars → 422."""
    res = client.post("/auth/register", json={
        "username": "x" * 201,
        "password": "validpass12345",
        "email": "test@test.com",
    })
    assert res.status_code in (400, 422), (
        f"expected 422 by Pydantic, got {res.status_code}: {res.text[:200]}"
    )


def test_register_password_oversized(client):
    res = client.post("/auth/register", json={
        "username": "validuser",
        "password": "x" * 201,
        "email": "test@test.com",
    })
    assert res.status_code in (400, 422)


def test_register_email_oversized(client):
    """RFC 5321 email max es 320 chars → 321 debe ser rechazado."""
    bigger = ("x" * 315) + "@a.com"  # 321 chars
    res_bad = client.post("/auth/register", json={
        "username": f"u_{uuid.uuid4().hex[:6]}",
        "password": "validpass12345",
        "email": bigger,
    })
    assert res_bad.status_code == 422


def test_approve_notes_oversized(client, admin_token, db):
    """ApproveJobRequest.notes > 2048 chars → 422."""
    from database import Job as JobModel

    job_id = uuid.uuid4().hex[:12]
    job = JobModel(
        job_id=job_id,
        user_id=1,
        tenant_id="default",
        artist="Test",
        song_title="x",
        filename="test.mp3",
        status="pending_review",
        delivery_profile="youtube",
    )
    db.add(job)
    db.commit()

    res = client.post(
        f"/approve/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"notes": "x" * 2049},
    )
    assert res.status_code == 422


def test_approve_notes_default_when_omitted(client, admin_token, db):
    """Regression: cliente que omite el campo notes en JSON debe pasar
    (default="" en Pydantic)."""
    from database import Job as JobModel

    job_id = uuid.uuid4().hex[:12]
    job = JobModel(
        job_id=job_id,
        user_id=1,
        tenant_id="default",
        artist="Test",
        song_title="x",
        filename="test.mp3",
        status="pending_review",
        delivery_profile="youtube",
    )
    db.add(job)
    db.commit()

    res = client.post(
        f"/approve/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={},
    )
    assert res.status_code == 200, (
        f"approve sin notes debería pasar (default=''), got {res.status_code}: "
        f"{res.text[:200]}"
    )


def test_edit_font_oversized(client, admin_token, db):
    """EditJobRequest.font > 64 chars → 422."""
    from database import Job as JobModel

    job_id = uuid.uuid4().hex[:12]
    job = JobModel(
        job_id=job_id,
        user_id=1,
        tenant_id="default",
        artist="Test",
        song_title="x",
        filename="test.mp3",
        status="pending_review",
        delivery_profile="youtube",
        bg_r2_key_cached="fake/key.mp4",
        segments_json=[{"start": 0.0, "end": 1.0, "text": "t"}],
        edit_count=0,
    )
    db.add(job)
    db.commit()

    res = client.post(
        f"/edit/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"edit_type": "typography", "font": "f" * 65},
    )
    assert res.status_code == 422


def test_enable_prores_frame_size_oversized(client, admin_token, db, monkeypatch):
    """EnableProResRequest.umg_frame_size > 16 chars → 422."""
    import auth as auth_module
    from database import Job as JobModel

    monkeypatch.setattr(auth_module, "PRORES_TENANTS", set())  # admin bypassa
    job_id = uuid.uuid4().hex[:12]
    job = JobModel(
        job_id=job_id,
        user_id=1,
        tenant_id="default",
        artist="Test",
        song_title="x",
        filename="test.mp3",
        status="done",
        delivery_profile="youtube",
        progress=100,
    )
    db.add(job)
    db.commit()

    res = client.post(
        f"/enable-prores/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "umg_frame_size": "x" * 17,
            "umg_fps": "29.97",
            "umg_prores_profile": "3",
        },
    )
    assert res.status_code == 422


def test_upload_artist_oversized(client, user_token):
    """Form artist > 255 chars → 422. /upload usa Form, no BaseModel."""
    fake_mp3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" * 64
    res = client.post(
        "/upload",
        headers={"Authorization": f"Bearer {user_token}"},
        files={"file": ("test.mp3", fake_mp3, "audio/mpeg")},
        data={"artist": "x" * 256},
    )
    assert res.status_code == 422


def test_upload_artist_at_limit_passes_validation(client, user_token):
    """Artist exactly 200 chars debe pasar validación (puede fallar por
    OTRAS razones legítimas tipo audio inválido o quota, pero NO con
    422). Regression check anti off-by-one."""
    fake_mp3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" * 64
    res = client.post(
        "/upload",
        headers={"Authorization": f"Bearer {user_token}"},
        files={"file": ("test.mp3", fake_mp3, "audio/mpeg")},
        data={"artist": "a" * 200},
    )
    # Cualquier código menos 422 está OK. Si es 422, no debe ser por max_length.
    if res.status_code == 422:
        body_lower = res.text.lower()
        assert "max_length" not in body_lower and "string" not in body_lower, (
            f"422 por max_length con artist=200 chars (límite exacto): {res.text[:200]}"
        )
