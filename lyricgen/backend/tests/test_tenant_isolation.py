"""Tenant isolation guarantees.

Two tenants must NOT see, download, modify, or delete each other's
jobs. The product depends on this for multi-label B2B operation
(e.g. one Universal team should not see Sony's jobs running on the
same DB).

Scenarios covered:
  - GET /jobs (list) — each tenant sees only its own.
  - GET /status/{job_id} — 404 across tenants.
  - DELETE /jobs/{job_id} — 404 across tenants.
  - POST /jobs/bulk-delete — silently skips other-tenant ids.
  - GET /provenance/{job_id} — 404 across tenants.

Plus the multi-user-same-tenant case the operator cares about for
UMG: two users that share `tenant_id` DO see the same job list.
"""

import uuid

from database import SessionLocal, Job
from auth import create_user, start_login_session
from tests.conftest import auth


def _seed_job(db, tenant_id: str, user_id: int, status: str = "done") -> str:
    """Drop a single Job row directly for the given tenant/user."""
    job_id = f"isol_{uuid.uuid4().hex[:8]}"
    db.add(Job(
        job_id=job_id,
        user_id=user_id,
        tenant_id=tenant_id,
        artist="Test Artist",
        filename="test.mp3",
        style="oscuro",
        status=status,
        current_step="done",
        progress=100,
        delivery_profile="youtube",
    ))
    db.commit()
    return job_id


def _make_user(db, tenant_id: str, username_prefix: str):
    user = create_user(
        db,
        username=f"{username_prefix}_{uuid.uuid4().hex[:6]}",
        password="testpass12345",
        email=None,
        tenant_id=tenant_id,
    )
    token = start_login_session(db, user)
    return user, token


def test_two_tenants_do_not_see_each_others_jobs(client):
    db = SessionLocal()
    try:
        user_a, token_a = _make_user(db, "tenant_alpha", "alpha")
        user_b, token_b = _make_user(db, "tenant_beta",  "beta")
        job_a = _seed_job(db, "tenant_alpha", user_a.id)
        job_b = _seed_job(db, "tenant_beta",  user_b.id)
    finally:
        db.close()

    # Each list endpoint shows only the caller's tenant jobs.
    list_a = client.get("/jobs", headers=auth(token_a))
    list_b = client.get("/jobs", headers=auth(token_b))
    assert list_a.status_code == 200
    assert list_b.status_code == 200
    ids_a = {j["job_id"] for j in list_a.json()}
    ids_b = {j["job_id"] for j in list_b.json()}
    assert job_a in ids_a
    assert job_b not in ids_a
    assert job_b in ids_b
    assert job_a not in ids_b


def test_status_404_across_tenants(client):
    db = SessionLocal()
    try:
        user_a, _ = _make_user(db, "tenant_gamma", "gamma")
        _, token_b = _make_user(db, "tenant_delta", "delta")
        job_a = _seed_job(db, "tenant_gamma", user_a.id)
    finally:
        db.close()

    res = client.get(f"/status/{job_a}", headers=auth(token_b))
    assert res.status_code in (403, 404), (
        f"expected 403/404 across tenants, got {res.status_code}"
    )


def test_delete_404_across_tenants(client):
    db = SessionLocal()
    try:
        user_a, _ = _make_user(db, "tenant_epsilon", "epsilon")
        _, token_b = _make_user(db, "tenant_zeta",   "zeta")
        job_a = _seed_job(db, "tenant_epsilon", user_a.id, status="error")
    finally:
        db.close()

    res = client.delete(f"/jobs/{job_a}", headers=auth(token_b))
    assert res.status_code in (403, 404), (
        f"expected 403/404 across tenants, got {res.status_code}"
    )

    # And the job must still exist for the rightful tenant.
    db = SessionLocal()
    try:
        assert db.query(Job).filter(Job.job_id == job_a).first() is not None
    finally:
        db.close()


def test_bulk_delete_skips_other_tenants(client):
    db = SessionLocal()
    try:
        user_a, _ = _make_user(db, "tenant_eta",   "eta")
        user_b, token_b = _make_user(db, "tenant_theta", "theta")
        job_a = _seed_job(db, "tenant_eta",   user_a.id, status="error")
        job_b = _seed_job(db, "tenant_theta", user_b.id, status="error")
    finally:
        db.close()

    res = client.post(
        "/jobs/bulk-delete",
        headers=auth(token_b),
        json={"job_ids": [job_a, job_b]},
    )
    assert res.status_code == 200
    body = res.json()
    deleted = set(body.get("deleted") or [])
    assert job_b in deleted, "tenant_theta's own job should be deleted"
    assert job_a not in deleted, "tenant_theta must NOT delete tenant_eta's job"

    db = SessionLocal()
    try:
        # tenant_eta's job survives.
        assert db.query(Job).filter(Job.job_id == job_a).first() is not None
    finally:
        db.close()


def test_provenance_404_across_tenants(client):
    db = SessionLocal()
    try:
        user_a, _ = _make_user(db, "tenant_iota",  "iota")
        _, token_b = _make_user(db, "tenant_kappa", "kappa")
        job_a = _seed_job(db, "tenant_iota", user_a.id)
    finally:
        db.close()

    res = client.get(f"/provenance/{job_a}", headers=auth(token_b))
    assert res.status_code in (403, 404), (
        f"expected 403/404 across tenants on /provenance, got {res.status_code}"
    )


def test_two_users_same_tenant_share_jobs(client):
    """The UMG case: 3 operators in the same workspace see each other's
    jobs. Tested with 2 users to keep the fixture small."""
    shared = "tenant_umg_test"
    db = SessionLocal()
    try:
        user_x, token_x = _make_user(db, shared, "umgx")
        _,      token_y = _make_user(db, shared, "umgy")
        job_x = _seed_job(db, shared, user_x.id)
    finally:
        db.close()

    list_y = client.get("/jobs", headers=auth(token_y))
    assert list_y.status_code == 200
    ids_y = {j["job_id"] for j in list_y.json()}
    assert job_x in ids_y, "teammate in same tenant should see the job"

    status_y = client.get(f"/status/{job_x}", headers=auth(token_y))
    assert status_y.status_code == 200, (
        "teammate in same tenant should be able to read status"
    )


# ---------------------------------------------------------------------------
# get_job() contract (UMG-launch hardening 2026-06-01)
# ---------------------------------------------------------------------------

def test_get_job_scope_filters_are_keyword_only():
    """tenant_id/user_id are keyword-only so a future positional call
    can't silently pass the tenant into the wrong slot. A positional
    third argument must raise TypeError at call time."""
    import pytest
    from jobs import get_job

    db = SessionLocal()
    try:
        with pytest.raises(TypeError):
            get_job(db, "any_job_id", "tenant_alpha")  # positional scope → reject
    finally:
        db.close()


def test_get_job_unscoped_call_logs_warning(caplog):
    """An unscoped get_job() (no tenant_id, no user_id) is a global
    lookup — legitimate only for admin/internal paths, which should use
    get_job_model() instead. We don't raise (would break admin paths)
    but we log loudly so a missing tenant filter shows up in review."""
    import logging
    from jobs import get_job

    db = SessionLocal()
    try:
        with caplog.at_level(logging.WARNING, logger="genly.jobs"):
            get_job(db, "job_that_does_not_exist")
        warnings = [r for r in caplog.records
                    if "without tenant/user scope" in r.getMessage()]
        assert warnings, "unscoped get_job() must log a warning"
    finally:
        db.close()


def test_get_job_scoped_call_does_not_log_warning(caplog):
    """The standard endpoint pattern (tenant_id passed) must stay silent —
    the warning is only for unscoped calls."""
    import logging
    from jobs import get_job

    db = SessionLocal()
    try:
        user_a, _ = _make_user(db, "tenant_scoped_ok", "scoped")
        job_a = _seed_job(db, "tenant_scoped_ok", user_a.id)
        with caplog.at_level(logging.WARNING, logger="genly.jobs"):
            found = get_job(db, job_a, tenant_id="tenant_scoped_ok")
        assert found is not None
        warnings = [r for r in caplog.records
                    if "without tenant/user scope" in r.getMessage()]
        assert not warnings, "scoped get_job() must not warn"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Cross-tenant para ADMINS (pedido CEO 2026-06-11): el rol admin es de
# plataforma — puede abrir el video de cualquier tenant (con audit trail);
# los usuarios comunes siguen aislados.
# ---------------------------------------------------------------------------

def _seed_foreign_job(db, jid, tenant="otro-tenant-x"):
    from database import Job, User
    # user_id es NOT NULL: colgamos el job del primer usuario que exista
    # (el bootstrap admin); lo que se prueba es el AISLAMIENTO POR TENANT.
    any_uid = db.query(User.id).order_by(User.id).first()[0]
    db.add(Job(job_id=jid, user_id=any_uid, tenant_id=tenant, artist="X",
               song_title="Cross", filename="x.mp3", status="done",
               s3_keys={"video": f"outputs/{jid}/video.mp4"}))
    db.commit()


def test_admin_can_read_other_tenant_job(client, admin_token, db):
    from tests.conftest import auth
    _seed_foreign_job(db, "xtenant00001")
    try:
        r = client.get("/status/xtenant00001", headers=auth(admin_token))
        assert r.status_code == 200, r.text
        assert r.json()["job_id"] == "xtenant00001"
    finally:
        from database import Job
        db.query(Job).filter(Job.job_id == "xtenant00001").delete(synchronize_session=False)
        db.commit()


def test_regular_user_still_isolated(client, user_token, db):
    from tests.conftest import auth
    _seed_foreign_job(db, "xtenant00002")
    try:
        r = client.get("/status/xtenant00002", headers=auth(user_token))
        assert r.status_code == 404
    finally:
        from database import Job
        db.query(Job).filter(Job.job_id == "xtenant00002").delete(synchronize_session=False)
        db.commit()


def test_admin_cross_tenant_media_token_is_audited(client, admin_token, db):
    from tests.conftest import auth
    from database import AuditLog, Job
    _seed_foreign_job(db, "xtenant00003")
    try:
        r = client.get("/media-token/xtenant00003/video", headers=auth(admin_token))
        assert r.status_code == 200, r.text
        rows = (
            db.query(AuditLog)
            .filter(AuditLog.action == "admin.cross_tenant_access")
            .order_by(AuditLog.id.desc())
            .limit(5)
            .all()
        )
        assert any(
            (e.detail or {}).get("job_id") == "xtenant00003"
            and (e.detail or {}).get("job_tenant") == "otro-tenant-x"
            for e in rows
        ), "falta el rastro de auditoría cross-tenant"
    finally:
        db.query(Job).filter(Job.job_id == "xtenant00003").delete(synchronize_session=False)
        db.query(AuditLog).filter(AuditLog.action == "admin.cross_tenant_access").delete(synchronize_session=False)
        db.commit()


# ---------------------------------------------------------------------------
# EDITOR cross-tenant para admins (soporte a clientes, jul-2026): el mismo
# rol admin que ya LEE cualquier tenant ahora también GUARDA la corrección
# (/jobs/{id}/save-segments). Sin esto el autoguardado del editor daba 404
# permanente ("No pudimos guardar") sobre un job ajeno. Usuarios comunes
# siguen aislados; el acceso queda auditado.
# ---------------------------------------------------------------------------

_SEG_BODY = {"segments": [{"start": 0.0, "end": 1.5, "text": "hola"}]}


def test_admin_can_save_segments_cross_tenant(client, admin_token, db):
    from tests.conftest import auth
    from database import Job
    _seed_foreign_job(db, "xtenant00010")
    try:
        r = client.post("/jobs/xtenant00010/save-segments",
                        json=_SEG_BODY, headers=auth(admin_token))
        assert r.status_code == 200, r.text
        db.expire_all()
        job = db.query(Job).filter(Job.job_id == "xtenant00010").first()
        assert job.segments_json and job.segments_json[0]["text"] == "hola"
    finally:
        db.query(Job).filter(Job.job_id == "xtenant00010").delete(synchronize_session=False)
        db.commit()


def test_regular_user_cannot_save_segments_cross_tenant(client, user_token, db):
    from tests.conftest import auth
    from database import Job
    _seed_foreign_job(db, "xtenant00011")
    try:
        r = client.post("/jobs/xtenant00011/save-segments",
                        json=_SEG_BODY, headers=auth(user_token))
        assert r.status_code == 404, r.text
    finally:
        db.query(Job).filter(Job.job_id == "xtenant00011").delete(synchronize_session=False)
        db.commit()


def test_admin_cross_tenant_save_segments_is_audited(client, admin_token, db):
    from tests.conftest import auth
    from database import AuditLog, Job
    _seed_foreign_job(db, "xtenant00012")
    try:
        r = client.post("/jobs/xtenant00012/save-segments",
                        json=_SEG_BODY, headers=auth(admin_token))
        assert r.status_code == 200, r.text
        rows = (
            db.query(AuditLog)
            .filter(AuditLog.action == "admin.cross_tenant_access")
            .order_by(AuditLog.id.desc())
            .limit(5)
            .all()
        )
        assert any(
            (e.detail or {}).get("job_id") == "xtenant00012"
            and (e.detail or {}).get("kind") == "save_segments"
            for e in rows
        ), "falta el rastro de auditoría del save-segments cross-tenant"
    finally:
        db.query(Job).filter(Job.job_id == "xtenant00012").delete(synchronize_session=False)
        db.query(AuditLog).filter(AuditLog.action == "admin.cross_tenant_access").delete(synchronize_session=False)
        db.commit()
