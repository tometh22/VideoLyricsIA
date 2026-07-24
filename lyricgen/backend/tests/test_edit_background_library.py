"""POST /edit edit_type="background_library" — swap a library asset sin IA.

La salida del loop no-convergente de regens Veo (incidente Gaby 2026-07-08,
job eaff5c7baf50: 3 regens sin control y sin camino de vuelta a biblioteca).
Contratos pineados acá:

  - requiere background_id (400 sin él)
  - NO consume edit slot (mismo mecanismo que metadata) y funciona con el
    cupo ya agotado (edit_count = _MAX_EDITS)
  - multi-escena → 400 (nunca pisar el timeline cacheado)
  - asset inexistente/inactivo → 404
  - edit_params lleva el asset resuelto (library_bg) al worker
  - tenant isolation: asset de OTRO tenant → 404 sin side effects
    (job intacto, sin AssetUsage)
"""
import uuid

from database import (
    AssetUsage,
    BackgroundAsset,
    Job as JobModel,
    User as UserModel,
)


def _create_pending_review_job(db, tenant_id, user_id, **extra):
    job_id = uuid.uuid4().hex[:12]
    fields = dict(
        job_id=job_id,
        user_id=user_id,
        tenant_id=tenant_id,
        artist="Test",
        song_title="BG Library Test",
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


def _seed_asset(db, owner_tenant_id=None, active=True):
    asset = BackgroundAsset(
        name="Test asset",
        filename="library/test_bg_lib.mp4",
        file_type="mp4",
        is_active=active,
        owner_tenant_id=owner_tenant_id,
    )
    db.add(asset)
    db.commit()
    return asset.id


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


def _cleanup(db, job_ids=(), asset_ids=()):
    if asset_ids:
        db.query(AssetUsage).filter(AssetUsage.asset_id.in_(asset_ids)).delete(synchronize_session=False)
        db.query(BackgroundAsset).filter(BackgroundAsset.id.in_(asset_ids)).delete(synchronize_session=False)
    if job_ids:
        db.query(JobModel).filter(JobModel.job_id.in_(job_ids)).delete(synchronize_session=False)
    db.commit()


def test_requires_background_id(client, admin_token, db):
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_pending_review_job(db, tenant_id, user_id)
    try:
        res = client.post(
            f"/edit/{job_id}",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"edit_type": "background_library"},
        )
        assert res.status_code == 400, res.text
        assert "background_id" in res.text
    finally:
        _cleanup(db, job_ids=[job_id])


def test_swap_forwards_asset_and_does_not_consume_slot(client, admin_token, db, monkeypatch):
    captured = _capture_enqueue_calls(monkeypatch)
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_pending_review_job(db, tenant_id, user_id)
    asset_id = _seed_asset(db)
    try:
        res = client.post(
            f"/edit/{job_id}",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"edit_type": "background_library", "background_id": asset_id},
        )
        assert res.status_code == 200, res.text
        assert len(captured) == 1
        lib = captured[0]["edit_params"].get("library_bg")
        assert lib and lib["asset_id"] == asset_id
        assert lib["bg_r2_key"] == "library/test_bg_lib.mp4"
        db.expire_all()
        job = db.query(JobModel).filter(JobModel.job_id == job_id).first()
        assert job.edit_count == 0, "library swap must NOT consume an edit slot"
        assert job.status == "editing"
        assert job.current_step == "video", "no Veo step for a library swap"
        # AssetUsage audit row registered by the resolver.
        usage = db.query(AssetUsage).filter(AssetUsage.asset_id == asset_id).all()
        assert len(usage) == 1 and usage[0].job_id == job_id
    finally:
        _cleanup(db, job_ids=[job_id], asset_ids=[asset_id])


def test_swap_allowed_with_exhausted_edit_quota(client, admin_token, db, monkeypatch):
    """El caso Gaby: 3 regens quemados — el swap de biblioteca sigue abierto.

    El cap admin-exempt no cubre esto (los operadores UMG no son admin);
    verificamos vía un usuario regular con edit_count=3."""
    _capture_enqueue_calls(monkeypatch)
    from pipeline import _MAX_EDITS
    # Usuario regular en su propio tenant.
    uname = f"libuser_{uuid.uuid4().hex[:6]}"
    res = client.post("/auth/register", json={
        "username": uname, "password": "testpass12345", "email": f"{uname}@test.com",
    })
    token = res.json()["token"]
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).json()
    job_id = _create_pending_review_job(db, me["tenant_id"], me["id"], edit_count=_MAX_EDITS)
    asset_id = _seed_asset(db)  # global asset
    try:
        res = client.post(
            f"/edit/{job_id}",
            headers={"Authorization": f"Bearer {token}"},
            json={"edit_type": "background_library", "background_id": asset_id},
        )
        assert res.status_code == 200, res.text
    finally:
        _cleanup(db, job_ids=[job_id], asset_ids=[asset_id])


def test_multiscene_job_rejected(client, admin_token, db):
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_pending_review_job(
        db, tenant_id, user_id,
        scene_plan={"scenes": [{"key": "s1"}]},
    )
    asset_id = _seed_asset(db)
    try:
        res = client.post(
            f"/edit/{job_id}",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"edit_type": "background_library", "background_id": asset_id},
        )
        assert res.status_code == 400, res.text
        assert "Escenas" in res.text
    finally:
        _cleanup(db, job_ids=[job_id], asset_ids=[asset_id])


def test_inactive_asset_404(client, admin_token, db):
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_pending_review_job(db, tenant_id, user_id)
    asset_id = _seed_asset(db, active=False)
    try:
        res = client.post(
            f"/edit/{job_id}",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"edit_type": "background_library", "background_id": asset_id},
        )
        assert res.status_code == 404, res.text
        db.expire_all()
        job = db.query(JobModel).filter(JobModel.job_id == job_id).first()
        assert job.status == "pending_review", "failed resolve must leave the job untouched"
    finally:
        _cleanup(db, job_ids=[job_id], asset_ids=[asset_id])


def test_cross_tenant_asset_404_no_side_effects(client, db, monkeypatch):
    """Tenant isolation: un usuario regular NO puede usar el asset exclusivo
    de otro tenant — 404 no-revelador, job intacto, sin AssetUsage."""
    captured = _capture_enqueue_calls(monkeypatch)
    uname = f"xtenant_{uuid.uuid4().hex[:6]}"
    res = client.post("/auth/register", json={
        "username": uname, "password": "testpass12345", "email": f"{uname}@test.com",
    })
    token = res.json()["token"]
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).json()
    job_id = _create_pending_review_job(db, me["tenant_id"], me["id"])
    asset_id = _seed_asset(db, owner_tenant_id="otro-tenant-exclusivo")
    try:
        res = client.post(
            f"/edit/{job_id}",
            headers={"Authorization": f"Bearer {token}"},
            json={"edit_type": "background_library", "background_id": asset_id},
        )
        assert res.status_code == 404, res.text
        db.expire_all()
        job = db.query(JobModel).filter(JobModel.job_id == job_id).first()
        assert job.status == "pending_review"
        assert job.edit_count == 0
        assert db.query(AssetUsage).filter(AssetUsage.asset_id == asset_id).count() == 0
        assert captured == [], "nothing may be enqueued on a denied asset"
    finally:
        _cleanup(db, job_ids=[job_id], asset_ids=[asset_id])
