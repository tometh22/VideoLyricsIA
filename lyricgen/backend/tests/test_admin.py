"""Tests for admin endpoints."""

from tests.conftest import auth


def test_admin_stats(client, admin_token):
    res = client.get("/admin/stats", headers=auth(admin_token))
    assert res.status_code == 200
    data = res.json()
    assert "users" in data
    assert "jobs" in data
    assert "revenue" in data
    assert "plans" in data


def test_admin_users(client, admin_token):
    res = client.get("/admin/users", headers=auth(admin_token))
    assert res.status_code == 200
    data = res.json()
    assert "total" in data
    assert "users" in data
    assert isinstance(data["users"], list)


def test_admin_create_user(client, admin_token):
    res = client.post("/admin/users", headers=auth(admin_token), json={
        "username": "admin_created",
        "password": "password123",
        "email": "admincreated@test.com",
        "plan_id": "250",
    })
    assert res.status_code == 200
    assert res.json()["plan"] == "250"


def test_admin_create_user_in_reserved_tenant(client, admin_token):
    """POST /admin/users must bypass the reserved-tenant guard.

    `auth.create_user` defaults `enforce_reserved=True` to protect the
    public /auth/register endpoint from a self-registered user landing
    in a system tenant (`default`, `admin`) or squatting on a B2B name
    (`umg`, `warner`). But the admin POST is the "admin seeding script"
    path that the helper's own docstring says should bypass — the
    admin is EXPLICITLY assigning the tenant.

    Regression: prior to this fix, admin couldn't create UMG operators
    because `tenant_id="umg"` got blocked by the same guard meant for
    the public funnel. UMG onboarding 2026-05-28 hit this on the live
    /admin/users path.
    """
    res = client.post("/admin/users", headers=auth(admin_token), json={
        "username": "umg_operator_test",
        "password": "password123",
        "email": "op@umg-test.com",
        "plan_id": "250",
        "tenant_id": "umg",
    })
    assert res.status_code == 200, res.text
    assert res.json()["tenant_id"] == "umg"


def test_admin_create_multiple_users_in_same_tenant(client, admin_token):
    """Admin must be able to place N users into a shared team tenant.

    The "tenant already exists" collision check in auth.create_user
    fires alongside the reserved-tenant guard (both inside the same
    `if enforce_reserved` block). It protects the public funnel from
    a user attaching themselves to a strangers tenant, but blocks the
    intended B2B model where the admin creates a team workspace
    (e.g. all UMG operators on tenant_id="universal_music"). This test
    pins that admin-driven team-workspace creation works.
    """
    # Create the first user — claims the tenant.
    res1 = client.post("/admin/users", headers=auth(admin_token), json={
        "username": "umusic_op_one",
        "password": "password123",
        "email": "one@umusic-test.com",
        "plan_id": "250",
        "tenant_id": "umusic_test_team",
    })
    assert res1.status_code == 200, res1.text
    assert res1.json()["tenant_id"] == "umusic_test_team"

    # Create the second user — must SUCCEED, joining the same tenant.
    res2 = client.post("/admin/users", headers=auth(admin_token), json={
        "username": "umusic_op_two",
        "password": "password123",
        "email": "two@umusic-test.com",
        "plan_id": "250",
        "tenant_id": "umusic_test_team",
    })
    assert res2.status_code == 200, res2.text
    assert res2.json()["tenant_id"] == "umusic_test_team"
    # Different users, same tenant — they will share visibility via
    # _job_scope in production. Confirm the IDs are distinct.
    assert res1.json()["id"] != res2.json()["id"]


def test_admin_update_user(client, admin_token):
    # Create user first
    create_res = client.post("/admin/users", headers=auth(admin_token), json={
        "username": "to_update",
        "password": "password123",
    })
    user_id = create_res.json()["id"]

    # Update plan
    res = client.patch(f"/admin/users/{user_id}", headers=auth(admin_token), json={
        "plan_id": "500",
    })
    assert res.status_code == 200
    assert res.json()["plan"] == "500"


def test_admin_disable_user(client, admin_token):
    create_res = client.post("/admin/users", headers=auth(admin_token), json={
        "username": "to_disable",
        "password": "password123",
    })
    user_id = create_res.json()["id"]

    res = client.patch(f"/admin/users/{user_id}", headers=auth(admin_token), json={
        "is_active": False,
    })
    assert res.status_code == 200
    assert res.json()["is_active"] is False


def test_admin_denied_for_regular_user(client, user_token):
    res = client.get("/admin/stats", headers=auth(user_token))
    assert res.status_code == 403


def test_admin_jobs(client, admin_token):
    res = client.get("/admin/jobs", headers=auth(admin_token))
    assert res.status_code == 200
    assert "total" in res.json()


def test_admin_invoices(client, admin_token):
    res = client.get("/admin/invoices", headers=auth(admin_token))
    assert res.status_code == 200


def test_admin_audit_log(client, admin_token):
    res = client.get("/admin/audit", headers=auth(admin_token))
    assert res.status_code == 200
    assert isinstance(res.json(), list)


def test_admin_search_users(client, admin_token):
    client.post("/admin/users", headers=auth(admin_token), json={
        "username": "searchable_user",
        "password": "password123",
        "email": "searchable@test.com",
    })
    res = client.get("/admin/users?search=searchable", headers=auth(admin_token))
    assert res.status_code == 200
    assert res.json()["total"] >= 1


# ---------------------------------------------------------------------------
# Cost dashboard endpoints
# ---------------------------------------------------------------------------


def test_admin_cost_dashboard_endpoint(client, admin_token):
    res = client.get("/admin/cost", headers=auth(admin_token))
    assert res.status_code == 200
    data = res.json()
    assert "since_days" in data
    assert "grand_total_cost" in data
    assert "grand_total_calls" in data
    assert "tenants" in data
    assert isinstance(data["tenants"], list)


def test_admin_cost_dashboard_custom_window(client, admin_token):
    res = client.get("/admin/cost?since_days=7", headers=auth(admin_token))
    assert res.status_code == 200
    assert res.json()["since_days"] == 7


def test_admin_cost_per_tenant_endpoint(client, admin_token):
    res = client.get("/admin/cost/default", headers=auth(admin_token))
    assert res.status_code == 200
    data = res.json()
    assert data["tenant_id"] == "default"
    assert "total_cost" in data
    assert "total_calls" in data
    assert "by_tool" in data


def test_admin_cost_denied_for_regular_user(client, user_token):
    res = client.get("/admin/cost", headers=auth(user_token))
    assert res.status_code == 403


# ---------------------------------------------------------------------------
# Per-user activity observability (/admin/activity)
# ---------------------------------------------------------------------------

import uuid as _uuid
from datetime import datetime, timedelta, timezone


def _register_activity_user(client):
    """Register a fresh user for activity tests; return (id, username, token)."""
    username = f"activity_{_uuid.uuid4().hex[:6]}"
    res = client.post("/auth/register", json={
        "username": username,
        "password": "testpass12345",
        "email": f"{username}@test.com",
    })
    token = res.json()["token"]
    me = client.get("/auth/me", headers=auth(token)).json()
    return me["id"], username, token


def _activity_row(payload, user_id):
    """Find one user's row in the /admin/activity response."""
    return next((u for u in payload["users"] if u["user_id"] == user_id), None)


def _cleanup_activity_seed(db, job_prefix, user_id=None):
    """Remove committed seed rows so aggregates don't leak across tests."""
    from database import Job, AuditLog, AIProvenance, AssetUsage, BackgroundAsset
    db.query(AIProvenance).filter(AIProvenance.job_id.like(f"{job_prefix}%")).delete(
        synchronize_session=False)
    db.query(AssetUsage).filter(AssetUsage.job_id.like(f"{job_prefix}%")).delete(
        synchronize_session=False)
    db.query(BackgroundAsset).filter(BackgroundAsset.name.like(f"{job_prefix}%")).delete(
        synchronize_session=False)
    db.query(Job).filter(Job.job_id.like(f"{job_prefix}%")).delete(synchronize_session=False)
    if user_id is not None:
        db.query(AuditLog).filter(AuditLog.user_id == user_id).delete(synchronize_session=False)
    db.commit()


def test_admin_activity_denied_for_regular_user(client, user_token):
    res = client.get("/admin/activity", headers=auth(user_token))
    assert res.status_code == 403


def test_admin_activity_basic_shape(client, admin_token):
    res = client.get("/admin/activity", headers=auth(admin_token))
    assert res.status_code == 200
    data = res.json()
    assert data["since_days"] == 30
    assert isinstance(data["users"], list)
    # El admin de bootstrap siempre existe → la lista nunca está vacía
    assert len(data["users"]) >= 1


def test_admin_activity_video_aggregates(client, admin_token, db):
    from database import Job
    uid, _, _ = _register_activity_user(client)
    now = datetime.now(timezone.utc)
    jobs = [
        Job(job_id="actv1a", user_id=uid, tenant_id="act-test", artist="A",
            song_title="S1", filename="a.mp3", status="done",
            approved_by=uid, approved_at=now),
        Job(job_id="actv1b", user_id=uid, tenant_id="act-test", artist="A",
            song_title="S2", filename="a.mp3", status="done"),
        Job(job_id="actv1c", user_id=uid, tenant_id="act-test", artist="A",
            song_title="S3", filename="a.mp3", status="error",
            error="Veo 3 rate limit exceeded after 4 retries"),
        Job(job_id="actv1d", user_id=uid, tenant_id="act-test", artist="A",
            song_title="S4", filename="a.mp3", status="processing"),
        # Los previews de fondo no son videos del usuario → excluidos
        Job(job_id="actv1e", user_id=uid, tenant_id="act-test", artist="A",
            song_title="S5", filename="a.mp3", status="bg_preview_video"),
    ]
    for j in jobs:
        db.add(j)
    db.commit()
    try:
        res = client.get("/admin/activity", headers=auth(admin_token))
        assert res.status_code == 200
        row = _activity_row(res.json(), uid)
        assert row is not None
        assert row["videos"]["total"] == 4  # bg_preview_* excluido
        assert row["videos"]["done"] == 2
        assert row["videos"]["approved"] == 1
        assert row["videos"]["failed"] == 1
        assert row["videos"]["in_progress"] == 1
        assert row["errors"]["count"] == 1
        assert row["errors"]["recent"][0]["job_id"] == "actv1c"
        assert "Veo 3" in row["errors"]["recent"][0]["error"]
        assert row["last_activity"] is not None
    finally:
        _cleanup_activity_seed(db, "actv1", uid)


def test_admin_activity_rework_signals(client, admin_token, db):
    from database import Job, AuditLog
    uid, _, _ = _register_activity_user(client)
    # Variante + re-render parcial
    db.add(Job(job_id="actv2a", user_id=uid, tenant_id="act-test", artist="B",
               song_title="Base", filename="b.mp3", status="done", edit_count=2))
    db.add(Job(job_id="actv2b", user_id=uid, tenant_id="act-test", artist="B",
               song_title="Base v2", filename="b.mp3", status="done",
               parent_job_id="actv2a"))
    # Abandonado y recreado: mismo artist+song_title, el primero nunca terminó
    db.add(Job(job_id="actv2c", user_id=uid, tenant_id="act-test", artist="B",
               song_title="Recreada", filename="b.mp3", status="error",
               error="ffprobe failed on output"))
    db.add(Job(job_id="actv2d", user_id=uid, tenant_id="act-test", artist="B",
               song_title="Recreada", filename="b.mp3", status="done"))
    # Ediciones / retries / correcciones manuales vía AuditLog
    db.add(AuditLog(user_id=uid, action="job.edit_request", detail={"edit_type": "lyrics"}))
    db.add(AuditLog(user_id=uid, action="job.edit_request", detail={"edit_type": "background"}))
    db.add(AuditLog(user_id=uid, action="job.retry", detail={"job_id": "actv2c"}))
    # Tres autosaves del editor sobre el MISMO job → cuenta como 1 job
    # corregido, no 3 retrabajos. (Regresión del bug "1011 retrabajos" visto
    # en staging 2026-06-02: el editor autoguarda y cada save es un evento.)
    db.add(AuditLog(user_id=uid, action="lyrics.segments_diff", detail={"job_id": "actv2a"}))
    db.add(AuditLog(user_id=uid, action="lyrics.segments_diff", detail={"job_id": "actv2a"}))
    db.add(AuditLog(user_id=uid, action="lyrics.segments_diff", detail={"job_id": "actv2a"}))
    db.commit()
    try:
        res = client.get("/admin/activity", headers=auth(admin_token))
        assert res.status_code == 200
        row = _activity_row(res.json(), uid)
        assert row is not None
        assert row["rework"]["variants"] == 1
        assert row["rework"]["rerendered_jobs"] == 1
        assert row["rework"]["total_edits"] == 2
        assert row["rework"]["edits_lyrics"] == 1
        assert row["rework"]["edits_background"] == 1
        assert row["rework"]["edits_typography"] == 0
        assert row["rework"]["retries"] == 1
        assert row["rework"]["corrected_jobs"] == 1  # 3 autosaves, 1 solo job
        assert row["rework"]["abandoned_recreated"] == 1
    finally:
        _cleanup_activity_seed(db, "actv2", uid)


def test_admin_activity_backgrounds_split(client, admin_token, db):
    from database import Job, AIProvenance, AssetUsage, BackgroundAsset
    uid, _, _ = _register_activity_user(client)
    db.add(Job(job_id="actv3a", user_id=uid, tenant_id="act-test", artist="C",
               song_title="BG split", filename="c.mp3", status="done"))
    asset = BackgroundAsset(name="actv3-asset", filename="library/actv3.mp4", file_type="mp4")
    db.add(asset)
    db.flush()
    db.add(AssetUsage(asset_id=asset.id, user_id=uid, tenant_id="act-test",
                      job_id="actv3a", mode="as_is"))
    db.add(AIProvenance(job_id="actv3a", step="video_bg",
                        tool_name="veo-3.1-generate-001",
                        tool_provider="google_vertex", prompt_sent="bg prompt"))
    db.commit()
    try:
        res = client.get("/admin/activity", headers=auth(admin_token))
        assert res.status_code == 200
        row = _activity_row(res.json(), uid)
        assert row is not None
        assert row["backgrounds"]["library"] == 1
        assert row["backgrounds"]["ai_generated"] == 1
        assert row["ai_cost_usd"] > 0
    finally:
        _cleanup_activity_seed(db, "actv3", uid)


def test_admin_activity_since_days_filter(client, admin_token, db):
    from database import Job
    uid, _, _ = _register_activity_user(client)
    old = Job(job_id="actv4a", user_id=uid, tenant_id="act-test", artist="D",
              song_title="Vieja", filename="d.mp3", status="done")
    old.created_at = datetime.now(timezone.utc) - timedelta(days=60)
    db.add(old)
    db.commit()
    try:
        res30 = client.get("/admin/activity?since_days=30", headers=auth(admin_token))
        row30 = _activity_row(res30.json(), uid)
        assert row30["videos"]["total"] == 0  # fuera de la ventana de 30 días

        res90 = client.get("/admin/activity?since_days=90", headers=auth(admin_token))
        row90 = _activity_row(res90.json(), uid)
        assert row90["videos"]["total"] == 1  # dentro de la ventana de 90 días
    finally:
        _cleanup_activity_seed(db, "actv4", uid)


def test_admin_activity_detail(client, admin_token, user_token, db):
    from database import Job, AuditLog
    uid, _, _ = _register_activity_user(client)
    db.add(Job(job_id="actv5a", user_id=uid, tenant_id="act-test", artist="E",
               song_title="Detalle", filename="e.mp3", status="done"))
    db.add(AuditLog(user_id=uid, action="job.download",
                    detail={"job_id": "actv5a", "file_type": "video", "source": "r2_redirect"}))
    db.add(AuditLog(user_id=uid, action="job.edit_request",
                    detail={"job_id": "actv5a", "edit_type": "lyrics"}))
    db.commit()
    try:
        res = client.get(f"/admin/activity/{uid}", headers=auth(admin_token))
        assert res.status_code == 200
        data = res.json()
        assert data["user"]["id"] == uid
        assert len(data["jobs"]) == 1
        assert data["jobs"][0]["job_id"] == "actv5a"
        assert len(data["downloads"]) == 1
        assert data["downloads"][0]["detail"]["file_type"] == "video"
        assert len(data["events"]) == 1
        assert data["events"][0]["action"] == "job.edit_request"
    finally:
        _cleanup_activity_seed(db, "actv5", uid)


def test_admin_activity_detail_not_found(client, admin_token):
    res = client.get("/admin/activity/99999999", headers=auth(admin_token))
    assert res.status_code == 404


def test_admin_activity_detail_denied_for_regular_user(client, user_token):
    res = client.get("/admin/activity/1", headers=auth(user_token))
    assert res.status_code == 403


def test_admin_activity_super_admin_allowlist(client, admin_token, monkeypatch):
    """Con SUPER_ADMIN_USERS seteado, solo los usuarios listados (por
    username o email) acceden a la observabilidad — incluso siendo admin."""
    # El admin de bootstrap ("admin") no está en la lista → 403
    monkeypatch.setenv("SUPER_ADMIN_USERS", "tomas@epical.digital,agus.cafisi")
    res = client.get("/admin/activity", headers=auth(admin_token))
    assert res.status_code == 403
    res = client.get("/admin/activity/1", headers=auth(admin_token))
    assert res.status_code == 403

    # Agregado por username → pasa (case-insensitive)
    monkeypatch.setenv("SUPER_ADMIN_USERS", "tomas@epical.digital, agus.cafisi, ADMIN")
    res = client.get("/admin/activity", headers=auth(admin_token))
    assert res.status_code == 200


def test_admin_activity_allowlist_unset_allows_any_admin(client, admin_token, monkeypatch):
    """Sin SUPER_ADMIN_USERS (dev/staging/tests) alcanza con role=admin."""
    monkeypatch.delenv("SUPER_ADMIN_USERS", raising=False)
    res = client.get("/admin/activity", headers=auth(admin_token))
    assert res.status_code == 200


def test_admin_jobs_includes_username(client, admin_token, db):
    """El pipeline del admin muestra quién creó cada job → /admin/jobs
    devuelve username (join con users) además del tenant."""
    from database import Job
    uid, username, _ = _register_activity_user(client)
    db.add(Job(job_id="actv6a", user_id=uid, tenant_id="act-test", artist="F",
               song_title="Pipeline", filename="f.mp3", status="processing"))
    db.commit()
    try:
        res = client.get("/admin/jobs?tenant_id=act-test", headers=auth(admin_token))
        assert res.status_code == 200
        jobs = res.json()["jobs"]
        row = next(j for j in jobs if j["job_id"] == "actv6a")
        assert row["username"] == username
        assert row["tenant_id"] == "act-test"
    finally:
        _cleanup_activity_seed(db, "actv6", uid)


def test_admin_jobs_excludes_bg_previews_by_default(client, admin_token, db):
    """El pipeline del admin no muestra los jobs fantasma de preview de fondo
    (artefactos del wizard) — aparecían como duplicados de cada video real."""
    from database import Job
    uid, _, _ = _register_activity_user(client)
    db.add(Job(job_id="actv7a", user_id=uid, tenant_id="act-test", artist="G",
               song_title="Real", filename="g.mp3", status="done"))
    db.add(Job(job_id="actv7b", user_id=uid, tenant_id="act-test", artist="G",
               song_title="Real", filename="g.mp3", status="bg_preview_done"))
    db.commit()
    try:
        # Por default: solo el video real
        res = client.get("/admin/jobs?tenant_id=act-test", headers=auth(admin_token))
        ids = [j["job_id"] for j in res.json()["jobs"]]
        assert "actv7a" in ids
        assert "actv7b" not in ids
        # Con status explícito: el preview sigue siendo accesible
        res = client.get("/admin/jobs?tenant_id=act-test&status=bg_preview_done",
                         headers=auth(admin_token))
        ids = [j["job_id"] for j in res.json()["jobs"]]
        assert ids == ["actv7b"]
    finally:
        _cleanup_activity_seed(db, "actv7", uid)


def test_wizard_presets_admin_gets_them(client, admin_token):
    """Admin recibe los presets internos del wizard (label + apply). Estos NO
    viajan en el bundle del frontend — solo se sirven acá, gateado por admin."""
    res = client.get("/admin/wizard-presets", headers=auth(admin_token))
    assert res.status_code == 200
    data = res.json()
    assert isinstance(data.get("presets"), list) and len(data["presets"]) >= 1
    p = data["presets"][0]
    assert p.get("key") and p.get("label") and isinstance(p.get("apply"), dict)
    # El apply debe traer los settings que el cliente mapea a setters.
    assert "batchDefaults" in p["apply"]


def test_wizard_presets_non_admin_forbidden(client, user_token):
    """Un usuario NO-admin no debe poder ni recibir el JSON del preset."""
    res = client.get("/admin/wizard-presets", headers=auth(user_token))
    assert res.status_code == 403


def test_wizard_presets_unauthenticated_rejected(client):
    """Sin token, 401/403 — nunca 200."""
    res = client.get("/admin/wizard-presets")
    assert res.status_code in (401, 403)
