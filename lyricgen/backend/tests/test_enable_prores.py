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
        user_id=1,
        tenant_id=tenant_id,
        artist="Test Artist",
        song_title="Test Song",
        filename="test.mp3",
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
            "umg_frame_size": "HD",
            "umg_fps": "29.97",
            "umg_prores_profile": "3",
        },
    )
    assert res.status_code == 403, f"expected 403, got {res.status_code}: {res.text[:200]}"
    assert "ProRes" in res.text or "Broadcast" in res.text


def test_admin_can_enable_prores_for_other_tenant(
    monkeypatch, client, admin_token, db,
):
    """El operador admin puede preparar ProRes sobre el job cross-tenant
    que abrió desde el panel, sin cambiar el owner ni el tenant del video."""
    monkeypatch.setattr(auth, "PRORES_TENANTS", {"some-other-tenant"})
    job_id = _create_done_youtube_job(db, tenant_id="some-other-tenant")

    with pytest.MonkeyPatch.context() as queue_patch:
        queue_patch.setattr(
            "main.enqueue_prores_prewarm",
            lambda *_args, **_kwargs: "rq-test",
        )
        res = client.post(
            f"/enable-prores/{job_id}",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "umg_frame_size": "HD",
                "umg_fps": "29.97",
                "umg_prores_profile": "3",
            },
        )

    assert res.status_code == 200, res.text
    db.expire_all()
    fresh = db.query(JobModel).filter(JobModel.job_id == job_id).first()
    assert fresh.tenant_id == "some-other-tenant"
    assert fresh.umg_spec["frame_size"] == "HD"


def test_regular_user_cannot_enable_prores_for_other_tenant(
    monkeypatch, client, user_token, db,
):
    monkeypatch.setattr(auth, "PRORES_TENANTS", {"default"})
    job_id = _create_done_youtube_job(db, tenant_id="some-other-tenant")

    res = client.post(
        f"/enable-prores/{job_id}",
        headers={"Authorization": f"Bearer {user_token}"},
        json={
            "umg_frame_size": "HD",
            "umg_fps": "29.97",
            "umg_prores_profile": "3",
        },
    )
    # El entitlement se evalúa antes del lookup tenant-scoped; según el
    # tenant del fixture puede cortar en 403 o esconder el job con 404.
    assert res.status_code in (403, 404)


def test_enable_prores_400_when_job_not_done(monkeypatch, client, admin_token, db):
    """No se puede habilitar ProRes sobre un job que todavía está
    procesando — la descarga inmediata fallaría por SOURCE_MISSING."""
    monkeypatch.setattr(auth, "PRORES_TENANTS", set())  # admin igual pasa por role=admin
    job_id = uuid.uuid4().hex[:12]
    # Job en processing, NO en done
    job = JobModel(
        job_id=job_id,
        user_id=1,
        tenant_id="default",  # admin default tenant
        artist="A",
        song_title="S",
        filename="test.mp3",
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
            "umg_frame_size": "HD",
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
            "umg_frame_size": "HD",
            "umg_fps": "29.97",
            "umg_prores_profile": "3",
        },
    )
    assert res.status_code == 200, f"expected 200, got {res.status_code}: {res.text[:200]}"
    body = res.json()
    assert body["ok"] is True
    assert body["job_id"] == job_id
    assert body["umg_spec"]["frame_size"] == "HD"
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
    assert fresh.umg_spec["frame_size"] == "HD"
    # delivery_profile NO debe cambiar — mantenemos el dato histórico.
    assert fresh.delivery_profile == "youtube"

    status = client.get(
        f"/status/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert status.status_code == 200
    assert status.json()["umg_spec"]["frame_size"] == "HD"
    assert status.json()["prores_ready"] is False


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
            "umg_frame_size": "HD",
            "umg_fps": "29.97",
            "umg_prores_profile": "3",
        },
    )
    assert res.status_code == 200
    db.expire_all()
    fresh = db.query(JobModel).filter(JobModel.job_id == job_id).first()
    assert fresh.umg_spec["frame_size"] == "HD"
    assert fresh.umg_spec["fps"] == pytest.approx(29.97)
    assert fresh.umg_spec["prores_profile"] == 3


# ---------------------------------------------------------------------------
# Qué variantes se PRE-generan
# ---------------------------------------------------------------------------
#
# Medido sobre `audit_log` de producción (jun a sep-2026):
#
#   umg_master   284 descargas · 95 jobs distintos  → 92% de las entregas vivas
#   umg_short      4 descargas ·  2 jobs distintos  → 2%, y CERO desde junio
#
# Pre-generar el short manda 0,58 GB por video a R2 (egress de Railway, $0,05/GB)
# y los deja ahí para siempre — el bucket no tiene regla de expiración y crece
# ~$15/mes de costo NUEVO cada mes. Por dos descargas en cuatro meses.

def test_el_short_no_se_pregenera_por_defecto(monkeypatch):
    """El master sí, el short no. Es la diferencia entre 92% y 2% de uso."""
    import importlib
    import queue_jobs
    monkeypatch.delenv("PRORES_PREWARM_TYPES", raising=False)
    importlib.reload(queue_jobs)
    assert queue_jobs.PRORES_PREWARM_TYPES == ("umg_master",)


def test_un_pedido_explicito_del_usuario_se_respeta_igual(monkeypatch):
    """`force=True` es alguien apretando Descargar: no se le niega nunca.

    Sin esta excepción, sacar el short de la pre-generación lo volvería
    INDESCARGABLE en vez de perezoso, que es un bug de producto, no un
    ahorro.
    """
    import importlib
    import queue_jobs
    monkeypatch.delenv("PRORES_PREWARM_TYPES", raising=False)
    importlib.reload(queue_jobs)

    llamadas = []

    class _Q:
        def enqueue(self, *a, **k):
            llamadas.append(k.get("args") or a)
            class _J:  # noqa: D401
                id = "rq-1"
            return _J()

    # `q_enterprise` sale de `_init_redis()`, no es atributo del módulo.
    monkeypatch.setattr(queue_jobs, "_init_redis", lambda: (None, None, _Q()))
    monkeypatch.setattr(queue_jobs, "queue_depth", lambda *_a, **_k: 0)

    # Sin force: el short se saltea.
    assert queue_jobs.enqueue_prores_prewarm("j1", "umg_short") is None
    # Con force: se encola igual.
    assert queue_jobs.enqueue_prores_prewarm("j1", "umg_short", force=True) is not None


def test_se_puede_volver_atras_sin_deploy(monkeypatch):
    """La lista es una env var: reponer el short no exige tocar código."""
    import importlib
    import queue_jobs
    monkeypatch.setenv("PRORES_PREWARM_TYPES", "umg_master,umg_short")
    importlib.reload(queue_jobs)
    try:
        assert "umg_short" in queue_jobs.PRORES_PREWARM_TYPES
    finally:
        monkeypatch.delenv("PRORES_PREWARM_TYPES", raising=False)
        importlib.reload(queue_jobs)
