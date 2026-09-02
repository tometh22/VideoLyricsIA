"""Lifecycle del fondo personalizado subido por el usuario (audit 2026-06-11).

Cubre los tres bugs del audit:
  1. Validación de archivo: solo se miraba la extensión — un HEIC renombrado
     .jpg entraba y reventaba a mitad de pipeline; sin cap de tamaño.
  2. bg_r2_key_cached nunca se seteaba para fondos humanos → los edits
     rápidos (typography/lyrics/metadata) devolvían 400.
  3. /retry "preservaba" el fondo pasando bg_r2_key pero el pipeline lo
     descartaba (background_path=None) → regeneraba con Veo pisando la
     imagen del usuario.
"""
import io
import os
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from tests.conftest import auth
from database import Job as JobModel


def _decode_user(client, token):
    return client.get("/auth/me", headers=auth(token)).json()


def _fake_upload(filename: str, payload: bytes):
    """Imita la parte de UploadFile que usa _save_custom_background."""
    return SimpleNamespace(filename=filename, file=io.BytesIO(payload))


JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
MP4_BYTES = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 64
HEIC_BYTES = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 64  # HEIC real: ftyp+heic


# ---------------------------------------------------------------------------
# 1. Validación: magic bytes + tamaño
# ---------------------------------------------------------------------------

def _write(tmp_path, name, payload):
    p = tmp_path / name
    p.write_bytes(payload)
    return str(p)


def test_validate_bg_accepts_real_formats(tmp_path):
    from main import _validate_background_file_on_disk
    _validate_background_file_on_disk("foto.jpg", _write(tmp_path, "a.jpg", JPEG_BYTES))
    _validate_background_file_on_disk("foto.png", _write(tmp_path, "a.png", PNG_BYTES))
    _validate_background_file_on_disk("clip.mp4", _write(tmp_path, "a.mp4", MP4_BYTES))
    _validate_background_file_on_disk("clip.mov", _write(tmp_path, "a.mov", MP4_BYTES))


def test_validate_bg_rejects_heic_renamed_as_jpg(tmp_path):
    from main import _validate_background_file_on_disk
    path = _write(tmp_path, "fake.jpg", HEIC_BYTES)
    with pytest.raises(HTTPException) as exc:
        _validate_background_file_on_disk("fake.jpg", path)
    assert exc.value.status_code == 400
    assert "JPEG" in exc.value.detail
    # El archivo inválido se borra — no puede seguir al pipeline.
    assert not os.path.exists(path)


def test_validate_bg_rejects_garbage_png_and_mp4(tmp_path):
    from main import _validate_background_file_on_disk
    with pytest.raises(HTTPException):
        _validate_background_file_on_disk("x.png", _write(tmp_path, "x.png", b"garbage" * 10))
    with pytest.raises(HTTPException):
        _validate_background_file_on_disk("x.mp4", _write(tmp_path, "x.mp4", b"garbage" * 10))


def test_validate_bg_rejects_oversized_image(tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(main, "MAX_BG_IMAGE_MB", 0)
    path = _write(tmp_path, "big.jpg", JPEG_BYTES * 100)
    with pytest.raises(HTTPException) as exc:
        main._validate_background_file_on_disk("big.jpg", path)
    assert "too large" in exc.value.detail


# ---------------------------------------------------------------------------
# 2. _save_custom_background: persiste bg_r2_key_cached
# ---------------------------------------------------------------------------

def test_save_custom_background_rejects_unknown_extension(tmp_path):
    from main import _save_custom_background
    with pytest.raises(HTTPException) as exc:
        _save_custom_background(_fake_upload("malo.gif", JPEG_BYTES), str(tmp_path), "j1", "t1")
    assert exc.value.status_code == 400
    assert "Unsupported background format" in exc.value.detail


def test_save_custom_background_without_storage(tmp_path):
    """Sin R2 (dev local): escribe a disco, valida, y no persiste key."""
    from main import _save_custom_background
    bg_path, bg_r2_key = _save_custom_background(
        _fake_upload("foto.jpg", JPEG_BYTES), str(tmp_path), "j1", "t1",
    )
    assert bg_path.endswith("bg_custom.jpg") and os.path.exists(bg_path)
    assert bg_r2_key is None


def test_save_custom_background_persists_cached_key(tmp_path, monkeypatch):
    """Con R2: la key del upload queda en bg_r2_key_cached — habilita los
    edits rápidos y la preservación del retry para fondos humanos."""
    import main
    monkeypatch.setattr(main.storage, "is_enabled", lambda: True)
    monkeypatch.setattr(
        main.storage, "upload_input",
        lambda path, tenant, job_id, fn: f"inputs/{tenant}/{job_id}/{fn}",
    )
    persisted = {}
    monkeypatch.setattr(main, "update_job", lambda job_id, **kw: persisted.update(kw))

    bg_path, bg_r2_key = main._save_custom_background(
        _fake_upload("foto.png", PNG_BYTES), str(tmp_path), "j2", "umg",
    )
    assert bg_r2_key == "inputs/umg/j2/bg_custom.png"
    assert persisted == {"bg_r2_key_cached": "inputs/umg/j2/bg_custom.png"}


# ---------------------------------------------------------------------------
# 3. /retry preserva el fondo humano + 4. edits rápidos habilitados
# ---------------------------------------------------------------------------

def test_retry_preserves_custom_background(client, user_token, db, monkeypatch):
    me = _decode_user(client, user_token)
    jid = uuid.uuid4().hex[:12]
    cached = f"inputs/{me['tenant_id']}/{jid}/bg_custom.jpg"
    db.add(JobModel(
        job_id=jid, user_id=me["id"], tenant_id=me["tenant_id"],
        artist="Test", song_title="BG Retry", filename="a.mp3",
        status="error", error="worker died mid-render",
        input_r2_key=f"inputs/{me['tenant_id']}/{jid}/a.mp3",
        bg_r2_key_cached=cached,
    ))
    db.commit()
    captured = {}
    monkeypatch.setattr("main.enqueue_pipeline", lambda **kw: captured.update(kw) or "rq")
    try:
        r = client.post(f"/retry/{jid}", headers=auth(user_token))
        assert r.status_code == 200, r.text
        assert captured.get("bg_r2_key") == cached
        assert r.json().get("preserved_background") is True
    finally:
        db.query(JobModel).filter(JobModel.job_id == jid).delete(synchronize_session=False)
        db.commit()


def test_retry_does_not_reuse_bg_blamed_by_validation(client, user_token, db, monkeypatch):
    """Si el fondo fue la causa del fallo (validation_failed), reusarlo
    repetiría el fallo — el retry debe regenerar (bg_r2_key=None)."""
    me = _decode_user(client, user_token)
    jid = uuid.uuid4().hex[:12]
    db.add(JobModel(
        job_id=jid, user_id=me["id"], tenant_id=me["tenant_id"],
        artist="Test", song_title="BG Blamed", filename="a.mp3",
        status="validation_failed", error="content policy: prominent face",
        input_r2_key=f"inputs/{me['tenant_id']}/{jid}/a.mp3",
        bg_r2_key_cached=f"inputs/{me['tenant_id']}/{jid}/bg_custom.jpg",
    ))
    db.commit()
    captured = {}
    monkeypatch.setattr("main.enqueue_pipeline", lambda **kw: captured.update(kw) or "rq")
    try:
        r = client.post(f"/retry/{jid}", headers=auth(user_token))
        assert r.status_code == 200, r.text
        assert captured.get("bg_r2_key") is None
        assert r.json().get("preserved_background") is False
    finally:
        db.query(JobModel).filter(JobModel.job_id == jid).delete(synchronize_session=False)
        db.commit()


def test_edit_typography_allowed_with_custom_image_background(client, user_token, db, monkeypatch):
    """ANTES del fix: bg_r2_key_cached quedaba NULL para fondos humanos y
    este request devolvía 400 'No cached background available' — el
    usuario no podía corregir letra/tipografía sin destruir su fondo."""
    import main
    monkeypatch.setattr(main, "enqueue_edit", lambda **kwargs: "test:noop")
    me = _decode_user(client, user_token)
    jid = uuid.uuid4().hex[:12]
    db.add(JobModel(
        job_id=jid, user_id=me["id"], tenant_id=me["tenant_id"],
        artist="Test", song_title="Edit Custom BG", filename="a.mp3",
        status="pending_review", progress=100,
        bg_r2_key_cached=f"inputs/{me['tenant_id']}/{jid}/bg_custom.jpg",
        segments_json=[{"start": 0.0, "end": 1.0, "text": "hola"}],
    ))
    db.commit()
    try:
        r = client.post(
            f"/edit/{jid}", headers=auth(user_token),
            json={"edit_type": "typography", "font": "Arial"},
        )
        assert r.status_code == 202, r.text
    finally:
        db.query(JobModel).filter(JobModel.job_id == jid).delete(synchronize_session=False)
        db.commit()


# ---------------------------------------------------------------------------
# 5. El pipeline deriva background_path del bg_r2_key del retry
# ---------------------------------------------------------------------------

def test_pipeline_derives_background_path_from_bg_r2_key():
    """La condición de descarga exige background_path; /retry manda None.
    Verificamos que el derive exista en el código (regresión textual:
    si alguien lo borra, el fondo preservado vuelve a descartarse en
    silencio). El comportamiento end-to-end se prueba arriba vía el
    contrato del endpoint."""
    import inspect
    import pipeline
    src = inspect.getsource(pipeline.run_pipeline)
    assert "if background_path is None and bg_r2_key:" in src
    assert "os.path.basename(bg_r2_key)" in src


# ---------------------------------------------------------------------------
# 6. Batch usage de biblioteca (incidente 2026-06-11: fan-out de 80 GETs)
# ---------------------------------------------------------------------------

def test_backgrounds_usage_batch_single_request(client, user_token, db):
    from database import AssetUsage, BackgroundAsset
    me = _decode_user(client, user_token)
    a1 = BackgroundAsset(name="batch-usage-1", filename="library/bu1.jpg", file_type="jpg")
    a2 = BackgroundAsset(name="batch-usage-2", filename="library/bu2.mp4", file_type="mp4")
    db.add_all([a1, a2]); db.flush()
    db.add_all([
        AssetUsage(asset_id=a1.id, user_id=me["id"], tenant_id=me["tenant_id"],
                   job_id="bu_job1", mode="as_is"),
        AssetUsage(asset_id=a1.id, user_id=me["id"], tenant_id=me["tenant_id"],
                   job_id="bu_job2", mode="variation"),
        # Uso de OTRO tenant: no debe filtrar al caller
        AssetUsage(asset_id=a2.id, user_id=me["id"], tenant_id="otro-tenant",
                   job_id="bu_job3", mode="as_is"),
    ])
    db.commit()
    try:
        r = client.get("/backgrounds/usage", headers=auth(user_token))
        assert r.status_code == 200, r.text
        usage = r.json()["usage"]
        u1 = usage[str(a1.id)] if str(a1.id) in usage else usage.get(a1.id)
        assert u1 and u1["use_count"] == 2
        assert u1["as_is_count"] == 1 and u1["variation_count"] == 1
        # a2 solo fue usado por otro tenant → ausente para este caller
        assert str(a2.id) not in usage and a2.id not in {int(k) for k in usage}
    finally:
        db.query(AssetUsage).filter(AssetUsage.job_id.like("bu_job%")).delete(synchronize_session=False)
        db.query(BackgroundAsset).filter(BackgroundAsset.name.like("batch-usage-%")).delete(synchronize_session=False)
        db.commit()
