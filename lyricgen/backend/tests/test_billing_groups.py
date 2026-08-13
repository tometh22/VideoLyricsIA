"""Tests de billing groups (cuota compartida entre tenants) y gestión de
usuarios del admin (mover tenant, eliminar).

Caso de negocio: Universal Music tiene dos equipos (Argentina y Chile) en
tenants separados — no se ven los videos entre sí — pero la cuenta paga UN
solo plan de 250/mes. billing_group="universal_music" hace que ambos
consuman del mismo pool.
"""
import uuid as _uuid
from datetime import datetime, timezone

from tests.conftest import auth
from database import Job, User
from auth import get_plan_usage


def _make_user(client, admin_token, username, tenant, plan="250", billing_group=""):
    res = client.post("/admin/users", headers=auth(admin_token), json={
        "username": username,
        "password": "testpass12345",
        "email": f"{_uuid.uuid4().hex[:8]}@test.com",
        "plan_id": plan,
        "tenant_id": tenant,
        "billing_group": billing_group,
    })
    assert res.status_code == 200, res.text
    return res.json()


def _approved_job(db, user_id, tenant, job_id):
    """Job aprobado este mes = consume cuota."""
    db.add(Job(
        job_id=job_id, user_id=user_id, tenant_id=tenant, artist="U",
        song_title=job_id, filename="u.mp3", status="done",
        approved_by=user_id, approved_at=datetime.now(timezone.utc),
    ))


def _cleanup(db, job_prefix, usernames):
    db.query(Job).filter(Job.job_id.like(f"{job_prefix}%")).delete(synchronize_session=False)
    for u in usernames:
        db.query(User).filter(User.username == u).delete(synchronize_session=False)
    db.commit()


# ---------------------------------------------------------------------------
# Cuota compartida
# ---------------------------------------------------------------------------

def test_shared_quota_across_tenants_same_billing_group(client, admin_token, db):
    """AR aprueba 2 + CL aprueba 1 → ambos ven used=3 (pool compartido)."""
    suffix = _uuid.uuid4().hex[:6]
    ar_user = _make_user(client, admin_token, f"bg_ar_{suffix}", f"bgtest_ar_{suffix}",
                         billing_group=f"bg_group_{suffix}")
    cl_user = _make_user(client, admin_token, f"bg_cl_{suffix}", f"bgtest_cl_{suffix}",
                         billing_group=f"bg_group_{suffix}")
    # 2 aprobados en AR + 1 en CL
    _approved_job(db, ar_user["id"], ar_user["tenant_id"], f"bgq{suffix}a")
    _approved_job(db, ar_user["id"], ar_user["tenant_id"], f"bgq{suffix}b")
    _approved_job(db, cl_user["id"], cl_user["tenant_id"], f"bgq{suffix}c")
    db.commit()
    try:
        # Ambos usuarios ven el MISMO conteo (3) aunque estén en tenants distintos
        usage_ar = get_plan_usage(db, ar_user["id"], ar_user["tenant_id"], "250",
                                  billing_group=f"bg_group_{suffix}")
        usage_cl = get_plan_usage(db, cl_user["id"], cl_user["tenant_id"], "250",
                                  billing_group=f"bg_group_{suffix}")
        assert usage_ar["used"] == 3
        assert usage_cl["used"] == 3
        assert usage_ar["limit"] == 250
    finally:
        _cleanup(db, f"bgq{suffix}", [ar_user["username"], cl_user["username"]])


def test_quota_independent_without_billing_group(client, admin_token, db):
    """Sin billing_group, cada tenant tiene su propio pool (comportamiento histórico)."""
    suffix = _uuid.uuid4().hex[:6]
    u1 = _make_user(client, admin_token, f"nq_a_{suffix}", f"nqtest_a_{suffix}")
    u2 = _make_user(client, admin_token, f"nq_b_{suffix}", f"nqtest_b_{suffix}")
    _approved_job(db, u1["id"], u1["tenant_id"], f"nqj{suffix}a")
    _approved_job(db, u1["id"], u1["tenant_id"], f"nqj{suffix}b")
    _approved_job(db, u2["id"], u2["tenant_id"], f"nqj{suffix}c")
    db.commit()
    try:
        usage_1 = get_plan_usage(db, u1["id"], u1["tenant_id"], "250")
        usage_2 = get_plan_usage(db, u2["id"], u2["tenant_id"], "250")
        assert usage_1["used"] == 2  # solo los de SU tenant
        assert usage_2["used"] == 1
    finally:
        _cleanup(db, f"nqj{suffix}", [u1["username"], u2["username"]])


def test_billing_group_in_auth_me(client, admin_token):
    """El billing_group viaja en /auth/me para que /usage lo use."""
    suffix = _uuid.uuid4().hex[:6]
    created = _make_user(client, admin_token, f"bgme_{suffix}", f"bgme_t_{suffix}",
                         billing_group="grupo_test")
    assert created["billing_group"] == "grupo_test"
    # Login como ese usuario y verificar /auth/me
    login = client.post("/auth/login", json={
        "username": created["username"], "password": "testpass12345",
    })
    me = client.get("/auth/me", headers=auth(login.json()["token"])).json()
    assert me["billing_group"] == "grupo_test"


# ---------------------------------------------------------------------------
# Mover de tenant (PATCH)
# ---------------------------------------------------------------------------

def test_admin_move_user_tenant_moves_jobs(client, admin_token, db):
    """Cambiar el tenant de un usuario mueve sus jobs con él (default)."""
    suffix = _uuid.uuid4().hex[:6]
    user = _make_user(client, admin_token, f"mv_{suffix}", f"mvtest_old_{suffix}")
    _approved_job(db, user["id"], user["tenant_id"], f"mvj{suffix}a")
    db.add(Job(job_id=f"mvj{suffix}b", user_id=user["id"], tenant_id=user["tenant_id"],
               artist="M", song_title="x", filename="m.mp3", status="processing"))
    db.commit()
    try:
        res = client.patch(f"/admin/users/{user['id']}", headers=auth(admin_token), json={
            "tenant_id": f"mvtest_new_{suffix}",
            "billing_group": "grupo_nuevo",
        })
        assert res.status_code == 200, res.text
        data = res.json()
        assert data["tenant_id"] == f"mvtest_new_{suffix}"
        assert data["billing_group"] == "grupo_nuevo"
        # Los jobs se movieron al tenant nuevo
        db.expire_all()
        moved = db.query(Job).filter(
            Job.job_id.like(f"mvj{suffix}%"), Job.tenant_id == f"mvtest_new_{suffix}",
        ).count()
        assert moved == 2
    finally:
        _cleanup(db, f"mvj{suffix}", [user["username"]])


def test_admin_move_user_tenant_without_jobs(client, admin_token, db):
    """move_jobs=False deja los jobs en el tenant viejo."""
    suffix = _uuid.uuid4().hex[:6]
    user = _make_user(client, admin_token, f"mvn_{suffix}", f"mvntest_old_{suffix}")
    _approved_job(db, user["id"], user["tenant_id"], f"mvnj{suffix}a")
    db.commit()
    try:
        res = client.patch(f"/admin/users/{user['id']}", headers=auth(admin_token), json={
            "tenant_id": f"mvntest_new_{suffix}",
            "move_jobs": False,
        })
        assert res.status_code == 200
        db.expire_all()
        stayed = db.query(Job).filter(
            Job.job_id == f"mvnj{suffix}a", Job.tenant_id == f"mvntest_old_{suffix}",
        ).count()
        assert stayed == 1
    finally:
        _cleanup(db, f"mvnj{suffix}", [user["username"]])


def test_admin_remove_billing_group_with_empty_string(client, admin_token, db):
    """billing_group='' en el PATCH saca al usuario del grupo (vuelve a None)."""
    suffix = _uuid.uuid4().hex[:6]
    user = _make_user(client, admin_token, f"rmbg_{suffix}", f"rmbg_t_{suffix}",
                      billing_group="grupo_x")
    try:
        res = client.patch(f"/admin/users/{user['id']}", headers=auth(admin_token), json={
            "billing_group": "",
        })
        assert res.status_code == 200
        assert res.json()["billing_group"] is None
    finally:
        _cleanup(db, "rmbg_none", [user["username"]])


# ---------------------------------------------------------------------------
# Eliminar usuario (DELETE)
# ---------------------------------------------------------------------------

def test_admin_delete_user_soft_deletes(client, admin_token, db):
    suffix = _uuid.uuid4().hex[:6]
    user = _make_user(client, admin_token, f"del_{suffix}", f"deltest_{suffix}")
    res = client.delete(f"/admin/users/{user['id']}", headers=auth(admin_token))
    assert res.status_code == 200
    assert res.json()["deleted"] is True
    # Verificar anonimización
    db.expire_all()
    row = db.query(User).filter(User.id == user["id"]).first()
    try:
        assert row.is_active is False
        assert row.username == f"deleted_{user['id']}"
        assert row.email is None
        # No puede loguearse más
        login = client.post("/auth/login", json={
            "username": user["username"], "password": "testpass12345",
        })
        assert login.status_code in (400, 401)
    finally:
        db.query(User).filter(User.id == user["id"]).delete(synchronize_session=False)
        db.commit()


def test_admin_cannot_delete_self(client, admin_token, admin_user_id):
    res = client.delete(f"/admin/users/{admin_user_id}", headers=auth(admin_token))
    assert res.status_code == 400


def test_admin_cannot_delete_another_admin(client, admin_token, db):
    """Borrar a otro admin requiere degradarlo primero (dos pasos a propósito)."""
    suffix = _uuid.uuid4().hex[:6]
    other_admin = _make_user(client, admin_token, f"adm2_{suffix}", f"adm2t_{suffix}")
    try:
        # Promover a admin
        client.patch(f"/admin/users/{other_admin['id']}", headers=auth(admin_token),
                     json={"role": "admin"})
        res = client.delete(f"/admin/users/{other_admin['id']}", headers=auth(admin_token))
        assert res.status_code == 400
        assert "degrad" in res.json()["detail"].lower()
    finally:
        db.query(User).filter(User.id == other_admin["id"]).delete(synchronize_session=False)
        db.commit()


def test_admin_delete_denied_for_regular_user(client, user_token):
    res = client.delete("/admin/users/1", headers=auth(user_token))
    assert res.status_code == 403
