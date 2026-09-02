"""GET /jobs — scope de tenant.

Bug 2026-07-03: para admins, _job_scope devolvía kwargs vacíos y
get_all_jobs caía en su default literal tenant_id="default" — el admin
veía el historial de ESE tenant (congelado) en vez del cross-tenant
prometido ("no aparecen mis últimos videos"). Los users comunes siguen
estrictamente scopeados a su tenant.

Los jobs seedeados se borran en finally: la DB de CI es compartida entre
archivos y filas pending_review sueltas corren los conteos de
test_admin_metrics (funnel).
"""
import uuid

from database import Job as JobModel

from tests.conftest import auth


def _register(client, prefix, domain="test.com"):
    username = f"{prefix}_{uuid.uuid4().hex[:6]}"
    r = client.post("/auth/register", json={
        "username": username, "password": "testpass12345",
        "email": f"{username}@{domain}",
    })
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    me = client.get("/auth/me", headers=auth(token)).json()
    return token, me["id"], me["tenant_id"]


def _seed_job(db, tenant_id, user_id, title):
    job_id = uuid.uuid4().hex[:12]
    db.add(JobModel(
        job_id=job_id, user_id=user_id, tenant_id=tenant_id,
        artist="A", song_title=title, filename="a.mp3",
        status="pending_review", delivery_profile="youtube", progress=100,
    ))
    db.commit()
    return job_id


def _cleanup(db, job_ids):
    db.query(JobModel).filter(JobModel.job_id.in_(job_ids)).delete(
        synchronize_session=False,
    )
    db.commit()


def test_admin_history_is_cross_tenant(client, admin_token, db):
    # Usuario real en OTRO tenant (el registro deriva un tenant propio
    # del username — FK de user_id válida, tenant distinto de "default").
    _tok, foreign_uid, foreign_tenant = _register(client, "cross")
    assert foreign_tenant != "default"
    seeded = []
    try:
        seeded.append(_seed_job(db, "default", 1, "propio"))
        seeded.append(_seed_job(db, foreign_tenant, foreign_uid, "ajeno"))

        res = client.get("/jobs", headers={"Authorization": f"Bearer {admin_token}"})
        assert res.status_code == 200
        ids = {j["job_id"] for j in res.json()}
        assert seeded[0] in ids
        assert seeded[1] in ids  # antes: solo tenant "default" → faltaba
    finally:
        _cleanup(db, seeded)


# ---------------------------------------------------------------------------
# Filtro de cuentas sintéticas en el Historial cross-tenant de admin (bug
# real, 2026-08-19): staging.genly.pro corre bots de preflight/smoke/E2E
# contra la DB real; un admin viendo Historial global veía el suyo enterrado
# bajo cientos de filas de esos bots ("no lo encuentro" aunque el job seguía
# vivo). `test.com` es la convención de la propia suite de tests (arriba,
# `_register`) y debe seguir visible; los dominios de bots reales de
# staging (`.local`/`.test`) no.
# ---------------------------------------------------------------------------

def test_admin_history_excludes_synthetic_domains(client, admin_token, db):
    _tok, bot_uid, bot_tenant = _register(client, "preflightbot", domain="synthetic.genly.test")
    seeded = []
    try:
        seeded.append(_seed_job(db, "default", 1, "propio"))
        seeded.append(_seed_job(db, bot_tenant, bot_uid, "bot de smoke test"))

        res = client.get("/jobs", headers={"Authorization": f"Bearer {admin_token}"})
        assert res.status_code == 200
        ids = {j["job_id"] for j in res.json()}
        assert seeded[0] in ids
        assert seeded[1] not in ids  # el bot no debe tapar el historial humano
    finally:
        _cleanup(db, seeded)


def test_synthetic_account_still_sees_its_own_history(client, db):
    # El filtro es solo para la vista GLOBAL de admin — el propio bot (o
    # cualquier script de CI que use esa cuenta) sigue viendo sus jobs.
    token, uid, tenant = _register(client, "preflightbot2", domain="synthetic.genly.test")
    seeded = []
    try:
        seeded.append(_seed_job(db, tenant, uid, "bot de smoke test"))

        res = client.get("/jobs", headers=auth(token))
        assert res.status_code == 200
        ids = {j["job_id"] for j in res.json()}
        assert seeded[0] in ids
    finally:
        _cleanup(db, seeded)


def test_regular_user_history_stays_tenant_scoped(client, db):
    token, uid, tenant = _register(client, "scoped")
    _tok2, other_uid, other_tenant = _register(client, "other")
    assert other_tenant != tenant
    seeded = []
    try:
        seeded.append(_seed_job(db, tenant, uid, "mio"))
        seeded.append(_seed_job(db, other_tenant, other_uid, "ajeno"))

        res = client.get("/jobs", headers=auth(token))
        assert res.status_code == 200
        ids = {j["job_id"] for j in res.json()}
        assert seeded[0] in ids
        assert seeded[1] not in ids
    finally:
        _cleanup(db, seeded)
