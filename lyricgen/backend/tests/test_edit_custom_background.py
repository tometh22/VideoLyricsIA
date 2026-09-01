"""POST /edit edit_type="custom" — fondo subido por el operador en EDICIÓN.

Restaura la opción "Subir el mío" que #970 había ocultado en el wizard de
edición porque el backend /edit no tenía este edit_type (era un no-op
silencioso: el operador "cambiaba el fondo" y aprobar decía "No cambiaste
nada").

El body de /edit es JSON y no puede transportar bytes: el browser sube el
archivo a R2 vía POST /edit/{job}/custom-background (multipart, mismo
_save_custom_background que create-time) y manda la key en
custom_background_r2_key. Contratos pineados acá:

  - /edit requiere custom_background_r2_key (400 sin él)
  - SEGURIDAD: la key debe pertenecer al job/tenant (prefijo
    inputs/{tenant}/{job}/) — una key ajena → 400, job intacto
  - custom SIN animar: no consume slot ($0, como library), current_step=video
  - custom animado (Veo image-to-video): consume slot, current_step=background
  - edit_params lleva custom_bg {bg_r2_key, animate_image} al worker
  - multi-escena → 400 (nunca pisar el timeline cacheado)
  - el endpoint de subida namespacea bajo inputs/{job.tenant_id}/{job}/ y
    NO persiste bg_r2_key_cached (persist_cache=False)
"""
import io
import uuid

from database import Job as JobModel, User as UserModel


def _create_pending_review_job(db, tenant_id, user_id, **extra):
    job_id = uuid.uuid4().hex[:12]
    fields = dict(
        job_id=job_id,
        user_id=user_id,
        tenant_id=tenant_id,
        artist="Test",
        song_title="Custom BG Test",
        filename="test.mp3",
        status="pending_review",
        delivery_profile="youtube",
        progress=100,
        bg_r2_key_cached="fake/bg.mp4",
        segments_json=[{"start": 0.0, "end": 1.0, "text": "hola"}],
        edit_count=0,
    )
    fields.update(extra)
    db.add(JobModel(**fields))
    db.commit()
    return job_id


def _admin_identity(db):
    admin = db.query(UserModel).filter(UserModel.username == "admin").first()
    return admin.id, admin.tenant_id


def _capture_enqueue_calls(monkeypatch):
    import main
    captured: list[dict] = []
    monkeypatch.setattr(
        main, "enqueue_edit",
        lambda **kwargs: (captured.append(kwargs), "test:noop")[1],
    )
    return captured


def _valid_key(tenant_id, job_id, ext="jpg"):
    from storage import _safe_filename
    return f"inputs/{_safe_filename(tenant_id)}/{_safe_filename(job_id)}/bg_custom.{ext}"


def _cleanup(db, job_ids=()):
    if job_ids:
        db.query(JobModel).filter(JobModel.job_id.in_(job_ids)).delete(synchronize_session=False)
    db.commit()


def test_requires_custom_background_r2_key(client, admin_token, db):
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_pending_review_job(db, tenant_id, user_id)
    try:
        res = client.post(
            f"/edit/{job_id}",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"edit_type": "custom"},
        )
        assert res.status_code == 400, res.text
        assert "custom_background_r2_key" in res.text
    finally:
        _cleanup(db, job_ids=[job_id])


def test_foreign_key_rejected_no_side_effects(client, admin_token, db, monkeypatch):
    """SEGURIDAD: una key que no pertenece a este job/tenant se rechaza sin
    tocar el job ni encolar (evita hornear un objeto ajeno en el video)."""
    captured = _capture_enqueue_calls(monkeypatch)
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_pending_review_job(db, tenant_id, user_id)
    try:
        res = client.post(
            f"/edit/{job_id}",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "edit_type": "custom",
                "custom_background_r2_key": "inputs/otro-tenant/otro-job/bg_custom.jpg",
            },
        )
        assert res.status_code == 400, res.text
        db.expire_all()
        job = db.query(JobModel).filter(JobModel.job_id == job_id).first()
        assert job.status == "pending_review"
        assert job.edit_count == 0
        assert captured == [], "nada se encola con una key ajena"
    finally:
        _cleanup(db, job_ids=[job_id])


def test_custom_as_is_forwards_and_does_not_consume_slot(client, admin_token, db, monkeypatch):
    captured = _capture_enqueue_calls(monkeypatch)
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_pending_review_job(db, tenant_id, user_id)
    key = _valid_key(tenant_id, job_id)
    try:
        res = client.post(
            f"/edit/{job_id}",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"edit_type": "custom", "custom_background_r2_key": key},
        )
        assert res.status_code == 202, res.text
        assert len(captured) == 1
        cbg = captured[0]["edit_params"].get("custom_bg")
        assert cbg and cbg["bg_r2_key"] == key
        assert cbg["animate_image"] is False
        db.expire_all()
        job = db.query(JobModel).filter(JobModel.job_id == job_id).first()
        assert job.edit_count == 0, "custom sin animar NO consume slot ($0, como library)"
        assert job.status == "editing"
        assert job.current_step == "video", "sin Veo cuando no se anima"
    finally:
        _cleanup(db, job_ids=[job_id])


def test_custom_animated_consumes_slot_and_runs_veo_step(client, admin_token, db, monkeypatch):
    captured = _capture_enqueue_calls(monkeypatch)
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_pending_review_job(db, tenant_id, user_id)
    key = _valid_key(tenant_id, job_id)
    try:
        res = client.post(
            f"/edit/{job_id}",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "edit_type": "custom",
                "custom_background_r2_key": key,
                "animate_image": True,
            },
        )
        assert res.status_code == 202, res.text
        cbg = captured[0]["edit_params"].get("custom_bg")
        assert cbg["animate_image"] is True
        db.expire_all()
        job = db.query(JobModel).filter(JobModel.job_id == job_id).first()
        assert job.edit_count == 1, "custom animado corre Veo → consume slot"
        assert job.current_step == "background", "image-to-video pasa por el paso Veo"
    finally:
        _cleanup(db, job_ids=[job_id])


def test_multiscene_job_rejected(client, admin_token, db):
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_pending_review_job(
        db, tenant_id, user_id,
        scene_plan={"scenes": [{"key": "s1"}]},
    )
    key = _valid_key(tenant_id, job_id)
    try:
        res = client.post(
            f"/edit/{job_id}",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"edit_type": "custom", "custom_background_r2_key": key},
        )
        assert res.status_code == 400, res.text
        assert "Escenas" in res.text
    finally:
        _cleanup(db, job_ids=[job_id])


def test_upload_endpoint_stores_and_returns_key(client, admin_token, db, monkeypatch):
    """POST /edit/{job}/custom-background sube a R2 y devuelve la key sin
    persistir bg_r2_key_cached (persist_cache=False), namespaceada bajo el
    tenant DEL JOB."""
    import main
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_pending_review_job(db, tenant_id, user_id)
    expected_key = _valid_key(tenant_id, job_id, ext="png")

    seen = {}

    def _fake_save(background_file, job_dir, jid, tid, persist_cache=True):
        seen["persist_cache"] = persist_cache
        seen["tenant_id"] = tid
        return f"{job_dir}/bg_custom.png", expected_key

    monkeypatch.setattr(main.storage, "is_enabled", lambda: True)
    monkeypatch.setattr(main, "_save_custom_background", _fake_save)
    try:
        res = client.post(
            f"/edit/{job_id}/custom-background",
            headers={"Authorization": f"Bearer {admin_token}"},
            files={"background_file": ("mi-foto.png", io.BytesIO(b"\x89PNG\r\n\x1a\n"), "image/png")},
        )
        assert res.status_code == 200, res.text
        assert res.json()["bg_r2_key"] == expected_key
        assert seen["persist_cache"] is False, "el edit NO debe pisar bg_r2_key_cached todavía"
        assert seen["tenant_id"] == tenant_id, "namespaceado bajo el tenant del job"
    finally:
        _cleanup(db, job_ids=[job_id])


def test_upload_endpoint_rejects_non_pending_review(client, admin_token, db):
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_pending_review_job(db, tenant_id, user_id, status="done")
    try:
        res = client.post(
            f"/edit/{job_id}/custom-background",
            headers={"Authorization": f"Bearer {admin_token}"},
            files={"background_file": ("mi-foto.png", io.BytesIO(b"\x89PNG"), "image/png")},
        )
        assert res.status_code == 400, res.text
        assert "pending_review" in res.text
    finally:
        _cleanup(db, job_ids=[job_id])
