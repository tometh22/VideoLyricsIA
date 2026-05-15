"""Guardia 409 youtube_already_published en POST /edit/{job_id}.

CONTEXTO
--------
La API de YouTube no permite reemplazar el archivo de un video ya
subido (solo metadata). Si /edit con edit_type="lyrics" corriera sobre
un job que tiene `youtube_data` poblado, el archivo en R2 se
sobreescribiría con el cut re-sincronizado pero YouTube seguiría
sirviendo el viejo — drift silencioso.

QUÉ VALIDA ESTE TEST
--------------------
1. job con youtube_data + lyrics edit sin opt-in → 409 con detail.code
   ='youtube_already_published' y detail.youtube_url poblado.
2. mismo job + allow_youtube_drift=true → 200, edit encolado (status
   pasa a 'editing', edit_count incrementa).
3. job sin youtube_data → 200 directo, sin tocar la guardia.
"""

import uuid

from database import Job as JobModel, User as UserModel


def _create_done_job(db, tenant_id, user_id, youtube_data=None):
    """Inserta un Job en status='done' listo para ser editado vía /edit
    edit_type='lyrics'. bg_r2_key_cached y segments_json deben estar
    poblados para pasar los checks de request_edit; user_id es NOT NULL.
    """
    job_id = uuid.uuid4().hex[:12]
    job = JobModel(
        job_id=job_id,
        user_id=user_id,
        tenant_id=tenant_id,
        artist="Test",
        song_title="YT Guard Test",
        filename="test.mp3",
        status="done",
        delivery_profile="youtube",
        progress=100,
        bg_r2_key_cached="fake/bg.mp4",
        segments_json=[{"start": 0.0, "end": 1.0, "text": "hola"}],
        edit_count=0,
        youtube_data=youtube_data,
    )
    db.add(job)
    db.commit()
    return job_id


def _admin_identity(db):
    """Devuelve (user_id, tenant_id) del admin creado por conftest."""
    admin = db.query(UserModel).filter(UserModel.username == "admin").first()
    assert admin is not None, "admin user not seeded — chequear auth.py:create_user"
    return admin.id, admin.tenant_id


def _noop_enqueue(monkeypatch):
    """Reemplaza main.enqueue_edit por un no-op para que la request
    devuelva 200 sin disparar el worker. Sin esto, la fallback de thread
    en queue_jobs.enqueue_edit arrancaría run_edit_pipeline contra R2
    real y se rompería el test por motivos no relacionados a la guardia.
    """
    import main
    monkeypatch.setattr(main, "enqueue_edit", lambda **kwargs: "test:noop")


def test_lyrics_edit_blocks_when_youtube_published(
    client, admin_token, db, monkeypatch,
):
    """job con youtube_data → /edit lyrics sin allow_youtube_drift → 409."""
    _noop_enqueue(monkeypatch)
    user_id, tenant_id = _admin_identity(db)
    yt_url = "https://www.youtube.com/watch?v=abc123"
    job_id = _create_done_job(
        db, tenant_id, user_id,
        youtube_data={"url": yt_url, "video_id": "abc123"},
    )

    res = client.post(
        f"/edit/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "edit_type": "lyrics",
            "segments": [{"start": 0.25, "end": 1.25, "text": "hola"}],
        },
    )
    assert res.status_code == 409, (
        f"esperado 409, hubo {res.status_code}. Body: {res.text}"
    )
    detail = res.json()["detail"]
    assert isinstance(detail, dict), f"detail debería ser dict, fue {type(detail)}"
    assert detail.get("code") == "youtube_already_published", detail
    assert detail.get("youtube_url") == yt_url, detail

    # Y el job NO debe haber pasado a editing — la guardia bloqueó antes
    # de tocar status/edit_count.
    db.expire_all()
    fresh = db.query(JobModel).filter(JobModel.job_id == job_id).first()
    assert fresh.status == "done", (
        f"status quedó en {fresh.status!r}; la guardia 409 no debe mutar el job"
    )
    assert (fresh.edit_count or 0) == 0


def test_lyrics_edit_proceeds_with_allow_youtube_drift(
    client, admin_token, db, monkeypatch,
):
    """allow_youtube_drift=true → opt-in explícito → 200 y edit encolado."""
    _noop_enqueue(monkeypatch)
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_done_job(
        db, tenant_id, user_id,
        youtube_data={"url": "https://youtu.be/xyz", "video_id": "xyz"},
    )

    res = client.post(
        f"/edit/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "edit_type": "lyrics",
            "segments": [{"start": 0.25, "end": 1.25, "text": "hola"}],
            "allow_youtube_drift": True,
        },
    )
    assert res.status_code == 200, (
        f"esperado 200 con opt-in, hubo {res.status_code}. Body: {res.text}"
    )
    body = res.json()
    assert body["ok"] is True
    assert body["edit_count"] == 1

    db.expire_all()
    fresh = db.query(JobModel).filter(JobModel.job_id == job_id).first()
    assert fresh.status == "editing", (
        f"esperado status='editing' tras dispatch, fue {fresh.status!r}"
    )
    assert fresh.edit_count == 1


def test_lyrics_edit_no_youtube_data_skips_guard(
    client, admin_token, db, monkeypatch,
):
    """job sin youtube_data → guardia 409 no aplica, edit avanza."""
    _noop_enqueue(monkeypatch)
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_done_job(db, tenant_id, user_id, youtube_data=None)

    res = client.post(
        f"/edit/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "edit_type": "lyrics",
            "segments": [{"start": 0.25, "end": 1.25, "text": "hola"}],
        },
    )
    assert res.status_code == 200, (
        f"esperado 200 sin youtube_data, hubo {res.status_code}. Body: {res.text}"
    )
    db.expire_all()
    fresh = db.query(JobModel).filter(JobModel.job_id == job_id).first()
    assert fresh.status == "editing"
    assert fresh.edit_count == 1
