"""Tests del panel Insights (CEO): adoption / overview / wizard +
/telemetry/events + flag is_super_admin + drill-down enriquecido."""
import uuid as _uuid
from datetime import datetime, timedelta, timezone

from tests.conftest import auth


def _register_user(client, tenant=None):
    username = f"insights_{_uuid.uuid4().hex[:6]}"
    res = client.post("/auth/register", json={
        "username": username,
        "password": "testpass12345",
        "email": f"{username}@test.com",
    })
    token = res.json()["token"]
    me = client.get("/auth/me", headers=auth(token)).json()
    return me["id"], username, token


def _cleanup(db, prefix, user_ids=()):
    from database import Job, AuditLog, AIProvenance, AssetUsage, BackgroundAsset, UiEvent
    db.query(AIProvenance).filter(AIProvenance.job_id.like(f"{prefix}%")).delete(
        synchronize_session=False)
    db.query(AssetUsage).filter(AssetUsage.job_id.like(f"{prefix}%")).delete(
        synchronize_session=False)
    db.query(BackgroundAsset).filter(BackgroundAsset.name.like(f"{prefix}%")).delete(
        synchronize_session=False)
    db.query(Job).filter(Job.job_id.like(f"{prefix}%")).delete(synchronize_session=False)
    for uid in user_ids:
        db.query(AuditLog).filter(AuditLog.user_id == uid).delete(synchronize_session=False)
        db.query(UiEvent).filter(UiEvent.user_id == uid).delete(synchronize_session=False)
    db.commit()


# ─────────────────────────────────────────────────────────────────────────
# Permisos
# ─────────────────────────────────────────────────────────────────────────

def test_insights_denied_for_regular_user(client, user_token):
    for path in ("/admin/insights/adoption", "/admin/insights/overview",
                 "/admin/insights/wizard"):
        assert client.get(path, headers=auth(user_token)).status_code == 403


def test_insights_denied_for_non_listed_admin(client, admin_token, monkeypatch):
    monkeypatch.setenv("SUPER_ADMIN_USERS", "tomas@epical.digital")
    assert client.get("/admin/insights/adoption", headers=auth(admin_token)).status_code == 403


def test_insights_open_for_admin_without_allowlist(client, admin_token, monkeypatch):
    monkeypatch.delenv("SUPER_ADMIN_USERS", raising=False)
    assert client.get("/admin/insights/adoption", headers=auth(admin_token)).status_code == 200


# ─────────────────────────────────────────────────────────────────────────
# is_super_admin en /auth/me
# ─────────────────────────────────────────────────────────────────────────

def test_auth_me_super_admin_flag_without_allowlist(client, admin_token, user_token, monkeypatch):
    monkeypatch.delenv("SUPER_ADMIN_USERS", raising=False)
    # Sin allowlist: todo admin es super admin (mismo fallback que el gate).
    me = client.get("/auth/me", headers=auth(admin_token)).json()
    assert me["is_super_admin"] is True
    me = client.get("/auth/me", headers=auth(user_token)).json()
    assert me["is_super_admin"] is False


def test_auth_me_super_admin_flag_with_allowlist(client, admin_token, monkeypatch):
    monkeypatch.setenv("SUPER_ADMIN_USERS", "alguien@otro.com")
    me = client.get("/auth/me", headers=auth(admin_token)).json()
    assert me["is_super_admin"] is False
    # Matchea por username, case-insensitive (el bootstrap admin es "admin").
    monkeypatch.setenv("SUPER_ADMIN_USERS", "ADMIN")
    me = client.get("/auth/me", headers=auth(admin_token)).json()
    assert me["is_super_admin"] is True


# ─────────────────────────────────────────────────────────────────────────
# Adoption
# ─────────────────────────────────────────────────────────────────────────

def test_adoption_aggregates_render_params(client, admin_token, db):
    from database import Job
    uid, _, _ = _register_user(client)
    db.add_all([
        Job(job_id="insa1a", user_id=uid, tenant_id="ins-test", artist="A",
            song_title="S1", filename="a.mp3", status="done", style="oscuro",
            render_params={"lyrics_animation": "karaoke", "font": "Anton",
                           "custom_colors": "rojo,negro"}),
        Job(job_id="insa1b", user_id=uid, tenant_id="ins-test", artist="A",
            song_title="S2", filename="a.mp3", status="done", style="neon",
            render_params={"lyrics_animation": "karaoke", "font": "Anton",
                           "line_transition": "slide_up"}),
        # Sin params: cuenta en total_jobs pero no en jobs_with_params
        Job(job_id="insa1c", user_id=uid, tenant_id="ins-test", artist="A",
            song_title="S3", filename="a.mp3", status="error", render_params=None),
        # bg_preview: excluido por completo
        Job(job_id="insa1d", user_id=uid, tenant_id="ins-test", artist="A",
            song_title="S4", filename="a.mp3", status="bg_preview_video",
            render_params={"lyrics_animation": "pop"}),
    ])
    db.commit()
    try:
        res = client.get("/admin/insights/adoption?tenant_id=ins-test",
                         headers=auth(admin_token))
        assert res.status_code == 200
        data = res.json()
        assert data["total_jobs"] == 3
        assert data["jobs_with_params"] == 2
        assert data["features"]["lyrics_animation"]["karaoke"] == 2
        assert data["features"]["line_transition"]["slide_up"] == 1
        assert data["features"]["line_transition"]["(default)"] == 1
        # insa1c no setea style → cae al default de columna "oscuro"
        assert data["features"]["style"]["oscuro"] == 2
        assert data["features"]["style"]["neon"] == 1
        assert data["font"][0] == {"value": "Anton", "count": 2}
        assert data["flags"]["custom_colors"] == 1
        # user_id filtra al mismo set en este seed
        res2 = client.get(f"/admin/insights/adoption?user_id={uid}",
                          headers=auth(admin_token))
        assert res2.json()["total_jobs"] == 3
    finally:
        _cleanup(db, "insa1", [uid])


def test_adoption_background_source(client, admin_token, db):
    from database import Job, AssetUsage, BackgroundAsset, AIProvenance
    uid, _, _ = _register_user(client)
    asset = BackgroundAsset(name="insa2-asset", filename="x.mp4", file_type="mp4")
    db.add(asset)
    db.flush()
    db.add_all([
        Job(job_id="insa2a", user_id=uid, tenant_id="ins-test2", artist="B",
            song_title="L1", filename="b.mp3", status="done"),
        Job(job_id="insa2b", user_id=uid, tenant_id="ins-test2", artist="B",
            song_title="L2", filename="b.mp3", status="done"),
        Job(job_id="insa2c", user_id=uid, tenant_id="ins-test2", artist="B",
            song_title="L3", filename="b.mp3", status="done"),
        AssetUsage(asset_id=asset.id, user_id=uid, tenant_id="ins-test2",
                   job_id="insa2a", mode="as_is"),
        AssetUsage(asset_id=asset.id, user_id=uid, tenant_id="ins-test2",
                   job_id="insa2b", mode="variation"),
        AIProvenance(job_id="insa2b", step="video_bg", tool_name="veo-3.1",
                     tool_provider="google_vertex", prompt_sent="p"),
        AIProvenance(job_id="insa2c", step="image_bg", tool_name="imagen-3.0",
                     tool_provider="google_vertex", prompt_sent="p"),
    ])
    db.commit()
    try:
        res = client.get("/admin/insights/adoption?tenant_id=ins-test2",
                         headers=auth(admin_token))
        src = res.json()["background_source"]
        assert src.get("library_as_is") == 1
        # La biblioteca gana sobre provenance (variation también llama a Veo)
        assert src.get("library_variation") == 1
        assert src.get("ai_image") == 1
    finally:
        _cleanup(db, "insa2", [uid])


# ─────────────────────────────────────────────────────────────────────────
# Overview
# ─────────────────────────────────────────────────────────────────────────

def test_overview_kpis_and_drilldown_lists(client, admin_token, db):
    from database import Job, AuditLog, User
    uid, username, _ = _register_user(client)
    # En prod el tenant del job coincide con el del usuario; el filtro de
    # AuditLog por tenant joinea User.tenant_id, así que el seed lo refleja.
    db.query(User).filter(User.id == uid).update({"tenant_id": "ins-ov"})
    db.commit()
    now = datetime.now(timezone.utc)
    db.add_all([
        Job(job_id="inso1a", user_id=uid, tenant_id="ins-ov", artist="C",
            song_title="O1", filename="c.mp3", status="done",
            approved_by=uid, approved_at=now, edit_count=1),
        Job(job_id="inso1b", user_id=uid, tenant_id="ins-ov", artist="C",
            song_title="O2", filename="c.mp3", status="error",
            error="render compositor crashed"),
        # Job de la ventana ANTERIOR (para wow_delta)
        Job(job_id="inso1c", user_id=uid, tenant_id="ins-ov", artist="C",
            song_title="O3", filename="c.mp3", status="done",
            created_at=now - timedelta(days=10)),
        AuditLog(user_id=uid, action="job.retry", detail={"job_id": "inso1b"}),
        # Dos autosaves del MISMO job → 1 solo corrected_job
        AuditLog(user_id=uid, action="lyrics.segments_diff", detail={"job_id": "inso1a"}),
        AuditLog(user_id=uid, action="lyrics.segments_diff", detail={"job_id": "inso1a"}),
    ])
    db.commit()
    try:
        res = client.get("/admin/insights/overview?days=7&tenant_id=ins-ov",
                         headers=auth(admin_token))
        assert res.status_code == 200
        data = res.json()
        k = data["kpis"]
        assert k["jobs_total"] == 2
        assert k["jobs_done"] == 1
        assert k["jobs_failed"] == 1
        assert k["jobs_prev_window"] == 1
        assert k["wow_delta"] == 1.0  # 2 vs 1
        assert k["retries"] == 1
        assert k["corrected_jobs"] == 1  # jobs distintos, no eventos
        assert k["edits_total"] == 1
        # Nivel tenant: sin lista de tenants, usuarios del tenant presentes
        assert data["tenants"] is None
        row = next(u for u in data["users"] if u["user_id"] == uid)
        assert row["username"] == username
        assert row["rework_events"] == 1 + 1 + 1  # edit + retry + corrected
        assert data["errors_by_category"]  # el error de render clasificado
        assert data["recent_errors"][0]["job_id"] == "inso1b"
        # Nivel app: lista de tenants presente e incluye el seed
        res_app = client.get("/admin/insights/overview?days=7", headers=auth(admin_token))
        tenants = res_app.json()["tenants"]
        assert any(t["tenant_id"] == "ins-ov" for t in tenants)
    finally:
        _cleanup(db, "inso1", [uid])


# ─────────────────────────────────────────────────────────────────────────
# Telemetry events + wizard funnel
# ─────────────────────────────────────────────────────────────────────────

def test_telemetry_events_inert_without_flag(client, user_token, monkeypatch):
    monkeypatch.delenv("TELEMETRY_ENABLED", raising=False)
    res = client.post("/telemetry/events", headers=auth(user_token),
                      json={"events": [{"type": "wizard.step", "data": {"step_to": 2}}]})
    assert res.status_code == 200
    assert res.json() == {"ok": True, "recorded": 0}


def test_telemetry_events_whitelist_and_cap(client, db, monkeypatch):
    from database import UiEvent
    monkeypatch.setenv("TELEMETRY_ENABLED", "true")
    uid, _, token = _register_user(client)
    events = (
        [{"type": "wizard.step", "data": {"step_from": 1, "step_to": 2}}]
        + [{"type": "evento.inventado", "data": {}}]      # fuera de whitelist
        + [{"type": "wizard.generate", "data": {"batch_size": 1}}] * 30  # cap 25
    )
    res = client.post("/telemetry/events", headers=auth(token), json={"events": events})
    assert res.status_code == 200
    # 25 del cap, menos el inventado descartado
    assert res.json()["recorded"] == 24
    try:
        stored = db.query(UiEvent).filter(UiEvent.user_id == uid).all()
        assert len(stored) == 24
        assert all(e.event_type in ("wizard.step", "wizard.generate") for e in stored)
    finally:
        db.query(UiEvent).filter(UiEvent.user_id == uid).delete(synchronize_session=False)
        db.commit()


def test_wizard_funnel_empty_then_populated(client, admin_token, db, monkeypatch):
    from database import UiEvent
    monkeypatch.setenv("TELEMETRY_ENABLED", "true")
    uid, _, _ = _register_user(client)

    res = client.get(f"/admin/insights/wizard?user_id={uid}", headers=auth(admin_token))
    assert res.json()["empty"] is True

    now = datetime.now(timezone.utc)
    # Sesión 1: llega a paso 5 y genera. Sesión 2 (gap > 30min): abandona en paso 2.
    db.add_all([
        UiEvent(user_id=uid, tenant_id="ins-wz", event_type="wizard.step",
                event_data={"step_from": 1, "step_to": 2}, created_at=now - timedelta(hours=3)),
        UiEvent(user_id=uid, tenant_id="ins-wz", event_type="wizard.step",
                event_data={"step_from": 2, "step_to": 5}, created_at=now - timedelta(hours=3) + timedelta(minutes=4)),
        UiEvent(user_id=uid, tenant_id="ins-wz", event_type="wizard.generate",
                event_data={"batch_size": 2, "mode": "reviewed"},
                created_at=now - timedelta(hours=3) + timedelta(minutes=10)),
        UiEvent(user_id=uid, tenant_id="ins-wz", event_type="wizard.scene_mode",
                event_data={"mode": "library"}, created_at=now - timedelta(minutes=20)),
        UiEvent(user_id=uid, tenant_id="ins-wz", event_type="wizard.step",
                event_data={"step_from": 1, "step_to": 2}, created_at=now - timedelta(minutes=19)),
    ])
    db.commit()
    try:
        res = client.get(f"/admin/insights/wizard?user_id={uid}", headers=auth(admin_token))
        data = res.json()
        assert data["empty"] is False
        assert data["sessions_total"] == 2
        assert data["sessions_generated"] == 1
        assert data["conversion"] == 0.5
        funnel = {f["step"]: f["reached"] for f in data["funnel"]}
        assert funnel[1] == 2 and funnel[2] == 2 and funnel[5] == 1
        assert data["abandon_by_step"]["2"] == 1
        assert data["scene_modes"]["library"] == 1
        assert data["p50_to_generate_s"] == 600.0
    finally:
        db.query(UiEvent).filter(UiEvent.user_id == uid).delete(synchronize_session=False)
        db.commit()


# ─────────────────────────────────────────────────────────────────────────
# Drill-down enriquecido
# ─────────────────────────────────────────────────────────────────────────

def test_activity_detail_enriched(client, admin_token, db, monkeypatch):
    from database import Job, AssetUsage, BackgroundAsset
    monkeypatch.delenv("TELEMETRY_ENABLED", raising=False)
    uid, _, _ = _register_user(client)
    asset = BackgroundAsset(name="insd1-asset", filename="y.mp4", file_type="mp4")
    db.add(asset)
    db.flush()
    db.add_all([
        Job(job_id="insd1a", user_id=uid, tenant_id="ins-dt", artist="D",
            song_title="D1", filename="d.mp3", status="done", style="claro",
            render_params={"font": "Anton", "lyrics_animation": "glow",
                           "custom_colors": "azul"}),
        # Job sin params → choices null (no rompe)
        Job(job_id="insd1b", user_id=uid, tenant_id="ins-dt", artist="D",
            song_title="D2", filename="d.mp3", status="error", render_params=None),
        AssetUsage(asset_id=asset.id, user_id=uid, tenant_id="ins-dt",
                   job_id="insd1a", mode="as_is"),
    ])
    db.commit()
    try:
        res = client.get(f"/admin/activity/{uid}", headers=auth(admin_token))
        assert res.status_code == 200
        data = res.json()
        jobs = {j["job_id"]: j for j in data["jobs"]}
        choices = jobs["insd1a"]["choices"]
        assert choices["font"] == "Anton"
        assert choices["lyrics_animation"] == "glow"
        assert choices["style"] == "claro"
        assert choices["has_custom_colors"] is True
        assert choices["background_source"] == "library_as_is"
        assert jobs["insd1b"]["choices"] is None
        # Extras
        assert data["sessions"] is None  # telemetría apagada
        assert isinstance(data["logins"], list) and len(data["logins"]) >= 1
        assert data["library_usage"][0]["name"] == "insd1-asset"
        assert data["library_usage"][0]["mode"] == "as_is"
    finally:
        _cleanup(db, "insd1", [uid])
