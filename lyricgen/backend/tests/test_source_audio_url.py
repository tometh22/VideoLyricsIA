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
    monkeypatch.setattr(storage, "object_exists", lambda key: True)
    signed_calls = []

    def signed_url(key, expiry_seconds=3600, **kwargs):
        signed_calls.append((key, kwargs))
        return f"https://r2.fake/{key}?sig=ok"

    monkeypatch.setattr(storage, "generate_signed_url", signed_url)
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
    assert signed_calls[0][1]["response_content_type"] == "audio/mpeg"


def test_source_audio_url_prefers_ready_content_addressed_preview(
    client, admin_token, db, monkeypatch,
):
    """A ready preview is signed directly and the original is untouched."""
    import storage

    digest = "c" * 64
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_job(db, tenant_id, user_id)
    row = db.query(JobModel).filter(JobModel.job_id == job_id).one()
    row.input_audio_sha256 = digest
    db.commit()
    calls = []
    probed = []

    monkeypatch.setattr(
        storage, "object_exists",
        lambda key: probed.append(key) or True,
    )
    monkeypatch.setattr(
        storage, "generate_signed_url",
        lambda key, expiry_seconds=3600, **kwargs: calls.append((key, kwargs))
        or f"https://r2.fake/{key}?sig=ok",
    )

    res = client.get(
        f"/jobs/{job_id}/source-audio-url",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["source"] == "editor_preview"
    assert body["preview_status"] == "ready"
    assert calls[0][0] == storage.editor_audio_preview_key(digest)
    assert calls[0][1]["response_content_type"] == "audio/mp4"
    assert probed == [storage.editor_audio_preview_key(digest)]


def test_source_audio_url_miss_falls_back_to_original_and_queues_once(
    client, admin_token, db, monkeypatch,
):
    """A cold preview never blocks the editor or replaces the source key."""
    import storage
    import queue_jobs

    digest = "d" * 64
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_job(db, tenant_id, user_id)
    row = db.query(JobModel).filter(JobModel.job_id == job_id).one()
    row.input_audio_sha256 = digest
    db.commit()
    queued = []

    def fake_enqueue(*args):
        queued.append(args)
        return {"status": "queued", "deduplicated": False}

    # Input exists, preview does not. The second call is still safe: the real
    # enqueue helper's Redis lock dedupes it, while this endpoint test proves
    # the non-blocking response and fallback contract.
    monkeypatch.setattr(
        storage, "object_exists",
        lambda key: key == row.input_r2_key,
    )
    monkeypatch.setattr(queue_jobs, "enqueue_editor_audio_preview", fake_enqueue)
    monkeypatch.setattr(
        storage, "generate_signed_url",
        lambda key, expiry_seconds=3600, **kwargs: f"https://r2.fake/{key}?sig=original",
    )

    res = client.get(
        f"/jobs/{job_id}/source-audio-url",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["source"] == "input"
    assert body["preview_status"] == "pending"
    assert body["preview_pending"] is True
    assert body["preview_retry_after_seconds"] == 5
    assert queued and queued[0][0] == row.input_r2_key


def test_source_audio_url_can_bypass_a_broken_preview_without_disabling_cache(
    client, admin_token, db, monkeypatch,
):
    import storage

    digest = "1" * 64
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_job(db, tenant_id, user_id)
    row = db.query(JobModel).filter(JobModel.job_id == job_id).one()
    row.input_audio_sha256 = digest
    db.commit()
    signed = []
    monkeypatch.setattr(storage, "object_exists", lambda key: True)
    monkeypatch.setattr(
        storage, "generate_signed_url",
        lambda key, expiry_seconds=3600, **kwargs: signed.append(key)
        or f"https://r2.fake/{key}?sig=original",
    )

    res = client.get(
        f"/jobs/{job_id}/source-audio-url?prefer_original=1",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["source"] == "input"
    assert signed == [row.input_r2_key]


def test_source_audio_url_rejects_foreign_tenant_before_preview_probe(
    client, user_token, db, monkeypatch,
):
    """A foreign user cannot use a known digest to probe/sign shared R2."""
    import storage

    admin_id, admin_tenant = _admin_identity(db)
    job_id = _create_job(db, admin_tenant, admin_id)
    probes = []
    monkeypatch.setattr(storage, "object_exists", lambda key: probes.append(key) or True)
    res = client.get(
        f"/jobs/{job_id}/source-audio-url",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert res.status_code == 404
    assert probes == []


def test_source_audio_url_404_when_no_input_key(client, admin_token, db, monkeypatch):
    """input_r2_key NULL (jobs viejos) → 404, no 500."""
    import storage
    monkeypatch.setattr(storage, "generate_signed_url",
                        lambda key, expiry_seconds=3600, **kwargs: "https://should/not/be/called")
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


def test_admin_can_load_cross_tenant_waveform(client, admin_token, db, monkeypatch):
    """Platform admin review must use the same cross-tenant scope as audio."""
    import storage
    import waveform_compute

    user_id, admin_tenant = _admin_identity(db)
    job_id = _create_job(db, "universal_chile", user_id)
    assert admin_tenant != "universal_chile"
    monkeypatch.setattr(storage, "is_enabled", lambda: True)
    monkeypatch.setattr(
        waveform_compute,
        "compute_and_cache_waveform",
        lambda _job_id, _key: {"peaks": [0.1, 0.7], "duration": 236.4},
    )

    res = client.get(
        f"/jobs/{job_id}/waveform",
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert res.status_code == 200, res.text
    assert res.json()["duration"] == 236.4
