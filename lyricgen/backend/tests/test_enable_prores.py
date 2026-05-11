"""Tests del endpoint POST /enable-prores/{job_id}.

Cubre el camino feliz (admin habilita ProRes retroactivo sobre un job
done MP4-only) y los rechazos: RBAC, ownership, status del job, params
inválidos. El transcoding en sí NO se ejecuta — el endpoint solo
persiste umg_spec + encola; la transcodificación es del worker.
"""

import os
import uuid
import pytest

import auth
from database import Job as JobModel


def _create_done_youtube_job(db, tenant_id="default", umg_spec=None):
    """Insert a job in `done` state with delivery_profile=youtube.

    Modela el caso real: la compañera subió audio con el profile por
    defecto y el render terminó OK. El job tiene MP4 pero no umg_spec.
    """
    job_id = uuid.uuid4().hex[:12]
    job = JobModel(
        job_id=job_id,
        tenant_id=tenant_id,
        artist="Test Artist",
        song_title="Test Song",
        status="done",
        delivery_profile="youtube",
        umg_spec=umg_spec,
        progress=100,
    )
    db.add(job)
    db.commit()
    return job_id


def test_enable_prores_requires_prores_access(monkeypatch, client, user_token, db):
    """Un user sin features.prores_export recibe 403 incluso si el job
    es suyo y está done."""
    monkeypatch.setattr(auth, "PRORES_TENANTS", set())
    # Creamos un job con el mismo tenant_id que el user_token (default)
    job_id = _create_done_youtube_job(db, tenant_id="default")

    res = client.post(
        f"/enable-prores/{job_id}",
        headers={"Authorization": f"Bearer {user_token}"},
        json={
            "umg_frame_size": "1920x1080",
            "umg_fps": "29.97",
            "umg_prores_profile": "3",
        },
    )
    assert res.status_code == 403, f"expected 403, got {res.status_code}: {res.text[:200]}"
    assert "ProRes" in res.text or "Broadcast" in res.text


def test_enable_prores_404_for_other_tenant(monkeypatch, client, admin_token, db):
    """Admin tiene access, pero el job pertenece a otro tenant → 404
    (no leakea info de existencia del job a usuarios sin acceso)."""
    monkeypatch.setattr(auth, "PRORES_TENANTS", {"some-other-tenant"})
    job_id = _create_done_youtube_job(db, tenant_id="some-other-tenant")

    res = client.post(
        f"/enable-prores/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "umg_frame_size": "1920x1080",
            "umg_fps": "29.97",
            "umg_prores_profile": "3",
        },
    )
    # admin tenant != "some-other-tenant" → query filter no encuentra el job
    assert res.status_code == 404, f"expected 404, got {res.status_code}: {res.text[:200]}"


def test_enable_prores_400_when_job_not_done(monkeypatch, client, admin_token, db):
    """No se puede habilitar ProRes sobre un job que todavía está
    procesando — la descarga inmediata fallaría por SOURCE_MISSING."""
    monkeypatch.setattr(auth, "PRORES_TENANTS", set())  # admin igual pasa por role=admin
    job_id = uuid.uuid4().hex[:12]
    # Job en processing, NO en done
    job = JobModel(
        job_id=job_id,
        tenant_id="default",  # admin default tenant
        artist="A",
        song_title="S",
        status="processing",
        delivery_profile="youtube",
        progress=50,
    )
    db.add(job)
    db.commit()

    res = client.post(
        f"/enable-prores/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "umg_frame_size": "1920x1080",
            "umg_fps": "29.97",
            "umg_prores_profile": "3",
        },
    )
    assert res.status_code == 400, f"expected 400, got {res.status_code}: {res.text[:200]}"
    assert "done" in res.text.lower() or "processing" in res.text.lower()


def test_enable_prores_400_invalid_params(monkeypatch, client, admin_token, db):
    """Frame size inválido es rechazado por _parse_umg_params /
    validate_umg_config con 400 antes de tocar la DB."""
    monkeypatch.setattr(auth, "PRORES_TENANTS", set())
    job_id = _create_done_youtube_job(db, tenant_id="default")

    res = client.post(
        f"/enable-prores/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "umg_frame_size": "9999x9999",  # no es un tamaño soportado
            "umg_fps": "29.97",
            "umg_prores_profile": "3",
        },
    )
    assert res.status_code == 400, f"expected 400, got {res.status_code}: {res.text[:200]}"


def test_enable_prores_happy_path_persists_umg_spec(monkeypatch, client, admin_token, db):
    """Admin habilita ProRes con specs broadcast estándar → 200, el
    umg_spec queda persistido en la fila del job, response incluye
    el umg_spec parseado."""
    monkeypatch.setattr(auth, "PRORES_TENANTS", set())
    job_id = _create_done_youtube_job(db, tenant_id="default")

    res = client.post(
        f"/enable-prores/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "umg_frame_size": "1920x1080",
            "umg_fps": "29.97",
            "umg_prores_profile": "3",
        },
    )
    assert res.status_code == 200, f"expected 200, got {res.status_code}: {res.text[:200]}"
    body = res.json()
    assert body["ok"] is True
    assert body["job_id"] == job_id
    assert body["umg_spec"]["frame_size"] == "1920x1080"
    assert body["umg_spec"]["fps"] == pytest.approx(29.97)
    assert body["umg_spec"]["prores_profile"] == 3
    # `enqueued` puede ser [] si Redis no está disponible en CI — no es
    # un fail. Lo que sí debe estar es el umg_spec persistido.
    assert "enqueued" in body

    # Verifico que la fila del job ahora tiene umg_spec.
    db.expire_all()  # invalida el caché del session
    fresh = db.query(JobModel).filter(JobModel.job_id == job_id).first()
    assert fresh is not None
    assert fresh.umg_spec is not None
    assert fresh.umg_spec["frame_size"] == "1920x1080"
    # delivery_profile NO debe cambiar — mantenemos el dato histórico.
    assert fresh.delivery_profile == "youtube"


def test_enable_prores_idempotent_overwrites_umg_spec(monkeypatch, client, admin_token, db):
    """Si el job ya tiene umg_spec y se vuelve a llamar con specs
    distintas, el umg_spec se sobreescribe. NOTA: si el .mov ya existe
    en disco/R2, `ensure_prores_exists` no re-transcoda (short-circuit
    en os.path.exists). Para el escenario MP4-only del producto (sin
    .mov previo), esto no aplica."""
    monkeypatch.setattr(auth, "PRORES_TENANTS", set())
    job_id = _create_done_youtube_job(
        db, tenant_id="default",
        umg_spec={"frame_size": "1280x720", "fps": 24.0, "prores_profile": 2},
    )

    res = client.post(
        f"/enable-prores/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "umg_frame_size": "1920x1080",
            "umg_fps": "29.97",
            "umg_prores_profile": "3",
        },
    )
    assert res.status_code == 200
    db.expire_all()
    fresh = db.query(JobModel).filter(JobModel.job_id == job_id).first()
    assert fresh.umg_spec["frame_size"] == "1920x1080"
    assert fresh.umg_spec["fps"] == pytest.approx(29.97)
    assert fresh.umg_spec["prores_profile"] == 3
