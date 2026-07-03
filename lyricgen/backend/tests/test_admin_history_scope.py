"""GET /jobs — scope de tenant.

Bug 2026-07-03: para admins, _job_scope devolvía kwargs vacíos y
get_all_jobs caía en su default literal tenant_id="default" — el admin
veía el historial de ESE tenant (congelado) en vez del cross-tenant
prometido ("no aparecen mis últimos videos"). Los users comunes siguen
estrictamente scopeados a su tenant.
"""
import uuid

from database import Job as JobModel

from tests.conftest import auth


def _seed_job(db, tenant_id, user_id, title):
    job_id = uuid.uuid4().hex[:12]
    db.add(JobModel(
        job_id=job_id, user_id=user_id, tenant_id=tenant_id,
        artist="A", song_title=title, filename="a.mp3",
        status="pending_review", delivery_profile="youtube", progress=100,
    ))
    db.commit()
    return job_id


def test_admin_history_is_cross_tenant(client, admin_token, db):
    own = _seed_job(db, "default", 1, "propio")
    foreign = _seed_job(db, "otro_tenant_xyz", 999, "ajeno")

    res = client.get("/jobs", headers={"Authorization": f"Bearer {admin_token}"})
    assert res.status_code == 200
    ids = {j["job_id"] for j in res.json()}
    assert own in ids
    assert foreign in ids  # antes: solo tenant "default" → faltaban estos


def test_regular_user_history_stays_tenant_scoped(client, db):
    username = f"scope_{uuid.uuid4().hex[:6]}"
    r = client.post("/auth/register", json={
        "username": username, "password": "testpass12345",
        "email": f"{username}@test.com",
    })
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    me = client.get("/auth/me", headers=auth(token)).json()

    mine = _seed_job(db, me["tenant_id"], me["id"], "mio")
    foreign = _seed_job(db, "otro_tenant_xyz", 999, "ajeno")

    res = client.get("/jobs", headers=auth(token))
    assert res.status_code == 200
    ids = {j["job_id"] for j in res.json()}
    assert mine in ids
    assert foreign not in ids
