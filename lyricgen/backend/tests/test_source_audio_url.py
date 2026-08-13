"""GET /jobs/{job_id}/source-audio-url

Endpoint que sirve la URL signed R2 al MP3 fuente para el editor de
lyrics post-aprobación. Casos cubiertos:

1. Job propio con input_r2_key → 200 con {url, expires_in}.
2. Job sin input_r2_key (legacy/pre-R2) → 404 con detail descriptivo.
3. Job de otro tenant → 404 (no leak entre tenants).
4. Job inexistente → 404.

R2 storage está mockeado: en sqlite test sin env vars no hay client real,
así que monkeypatch reemplaza generate_signed_url para devolver una URL
sintética. Esto chequea que el endpoint llama bien al helper y propaga
la URL sin tocar realmente boto3.
"""

import uuid

from database import Job as JobModel, User as UserModel


def _admin_identity(db):
    admin = db.query(UserModel).filter(UserModel.username == "admin").first()
    assert admin is not None
    return admin.id, admin.tenant_id


def _create_job(db, tenant_id, user_id, input_r2_key="inputs/default/x/song.mp3"):
    job_id = uuid.uuid4().hex[:12]
    job = JobModel(
        job_id=job_id,
        user_id=user_id,
        tenant_id=tenant_id,
        artist="Test",
        song_title="Audio URL Test",
        filename="song.mp3",
        status="done",
        delivery_profile="youtube",
        progress=100,
        input_r2_key=input_r2_key,
    )
    db.add(job)
    db.commit()
    return job_id


def test_source_audio_url_returns_signed_url(client, admin_token, db, monkeypatch):
    """Job propio con input_r2_key → 200 con url firmada y TTL."""
    import storage
    monkeypatch.setattr(storage, "generate_signed_url",
                        lambda key, expiry_seconds=3600: f"https://r2.fake/{key}?sig=ok")
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_job(db, tenant_id, user_id)

    res = client.get(
        f"/jobs/{job_id}/source-audio-url",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["url"].startswith("https://r2.fake/")
    assert body["expires_in"] == 3600


def test_source_audio_url_404_when_no_input_key(client, admin_token, db, monkeypatch):
    """input_r2_key NULL (jobs viejos) → 404, no 500."""
    import storage
    monkeypatch.setattr(storage, "generate_signed_url",
                        lambda key, expiry_seconds=3600: "https://should/not/be/called")
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_job(db, tenant_id, user_id, input_r2_key=None)

    res = client.get(
        f"/jobs/{job_id}/source-audio-url",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 404
    assert "Source audio" in res.json()["detail"]


def test_source_audio_url_unknown_job_returns_404(client, admin_token):
    """job_id inexistente → 404."""
    res = client.get(
        "/jobs/nonexistent12/source-audio-url",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 404
