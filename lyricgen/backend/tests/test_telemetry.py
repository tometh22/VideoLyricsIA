"""Tests del heartbeat de telemetría de sesiones (POST /telemetry/heartbeat).

Contrato:
  - Con TELEMETRY_ENABLED apagada (default): responde 200 recorded=false,
    NO escribe nada (inerte — así se shipea).
  - Con la flag prendida: crea una sesión, la extiende si el gap es < 30
    min, crea una nueva si el gap es mayor.
  - Nunca 5xx, requiere auth.
  - El feature flag llega al frontend vía features.telemetry de /auth/me.
"""
from datetime import datetime, timedelta, timezone

from tests.conftest import auth
from database import UserSession


def _user_id(client, token):
    return client.get("/auth/me", headers=auth(token)).json()["id"]


def _cleanup_sessions(db, user_id):
    db.query(UserSession).filter(UserSession.user_id == user_id).delete(
        synchronize_session=False)
    db.commit()


def test_heartbeat_requires_auth(client):
    res = client.post("/telemetry/heartbeat")
    assert res.status_code in (401, 403)


def test_heartbeat_inert_when_flag_off(client, user_token, db, monkeypatch):
    monkeypatch.delenv("TELEMETRY_ENABLED", raising=False)
    uid = _user_id(client, user_token)
    res = client.post("/telemetry/heartbeat", headers=auth(user_token))
    assert res.status_code == 200
    assert res.json() == {"ok": True, "recorded": False}
    assert db.query(UserSession).filter(UserSession.user_id == uid).count() == 0


def test_heartbeat_creates_and_extends_session(client, user_token, db, monkeypatch):
    monkeypatch.setenv("TELEMETRY_ENABLED", "1")
    uid = _user_id(client, user_token)
    try:
        # Primer heartbeat → crea sesión
        res = client.post("/telemetry/heartbeat", headers=auth(user_token))
        assert res.status_code == 200
        assert res.json() == {"ok": True, "recorded": True}
        sessions = db.query(UserSession).filter(UserSession.user_id == uid).all()
        assert len(sessions) == 1
        assert sessions[0].heartbeats == 1

        # Segundo heartbeat dentro de los 30 min → extiende la misma sesión
        res = client.post("/telemetry/heartbeat", headers=auth(user_token))
        assert res.status_code == 200
        db.expire_all()
        sessions = db.query(UserSession).filter(UserSession.user_id == uid).all()
        assert len(sessions) == 1
        assert sessions[0].heartbeats == 2
        assert sessions[0].last_seen_at >= sessions[0].started_at
    finally:
        _cleanup_sessions(db, uid)


def test_heartbeat_starts_new_session_after_gap(client, user_token, db, monkeypatch):
    monkeypatch.setenv("TELEMETRY_ENABLED", "1")
    uid = _user_id(client, user_token)
    try:
        client.post("/telemetry/heartbeat", headers=auth(user_token))
        # Simular que la última sesión quedó vieja (> 30 min de gap)
        old = datetime.now(timezone.utc) - timedelta(minutes=45)
        session = db.query(UserSession).filter(UserSession.user_id == uid).first()
        session.started_at = old - timedelta(minutes=10)
        session.last_seen_at = old
        db.commit()

        client.post("/telemetry/heartbeat", headers=auth(user_token))
        db.expire_all()
        sessions = (
            db.query(UserSession)
            .filter(UserSession.user_id == uid)
            .order_by(UserSession.started_at)
            .all()
        )
        assert len(sessions) == 2
        # La vieja quedó intacta; la nueva arranca con 1 heartbeat
        assert sessions[0].heartbeats == 1
        assert sessions[1].heartbeats == 1
    finally:
        _cleanup_sessions(db, uid)


def test_telemetry_feature_flag_in_auth_me(client, user_token, monkeypatch):
    """El frontend decide si manda heartbeats según features.telemetry."""
    monkeypatch.delenv("TELEMETRY_ENABLED", raising=False)
    me = client.get("/auth/me", headers=auth(user_token)).json()
    assert me["features"]["telemetry"] is False

    monkeypatch.setenv("TELEMETRY_ENABLED", "true")
    me = client.get("/auth/me", headers=auth(user_token)).json()
    assert me["features"]["telemetry"] is True


def test_admin_activity_includes_sessions_when_enabled(client, admin_token, user_token, db, monkeypatch):
    """/admin/activity expone sessions (online, tiempo hoy/semana) con la flag on."""
    monkeypatch.setenv("TELEMETRY_ENABLED", "1")
    uid = _user_id(client, user_token)
    try:
        client.post("/telemetry/heartbeat", headers=auth(user_token))
        res = client.get("/admin/activity", headers=auth(admin_token))
        assert res.status_code == 200
        data = res.json()
        assert data["telemetry_enabled"] is True
        row = next(u for u in data["users"] if u["user_id"] == uid)
        assert row["sessions"] is not None
        assert row["sessions"]["online"] is True
        assert row["sessions"]["sessions_week"] == 1
        assert row["sessions"]["seconds_week"] >= 0
        # La sesión también cuenta como última actividad
        assert row["last_activity"] is not None
    finally:
        _cleanup_sessions(db, uid)


def test_admin_activity_sessions_null_when_disabled(client, admin_token, user_token, monkeypatch):
    monkeypatch.delenv("TELEMETRY_ENABLED", raising=False)
    res = client.get("/admin/activity", headers=auth(admin_token))
    assert res.status_code == 200
    data = res.json()
    assert data["telemetry_enabled"] is False
    assert all(u["sessions"] is None for u in data["users"])


def test_admin_activity_errors_by_category(client, admin_token, user_token, db):
    """El breakdown por categoría usa la columna error_category y cae al
    clasificador de texto para rows sin columna (históricas)."""
    from database import Job
    uid = _user_id(client, user_token)
    # Un error con categoría persistida + uno histórico solo con texto
    db.add(Job(job_id="telcat1", user_id=uid, tenant_id="tel-test", artist="X",
               song_title="Con columna", filename="x.mp3", status="error",
               error="lo que sea", error_category="veo"))
    db.add(Job(job_id="telcat2", user_id=uid, tenant_id="tel-test", artist="X",
               song_title="Sin columna", filename="x.mp3", status="error",
               error="ffprobe failed on render: bad stream"))
    db.commit()
    try:
        res = client.get("/admin/activity", headers=auth(admin_token))
        assert res.status_code == 200
        data = res.json()
        assert data["errors_by_category"].get("veo", 0) >= 1
        assert data["errors_by_category"].get("render", 0) >= 1
        row = next(u for u in data["users"] if u["user_id"] == uid)
        assert row["errors"]["by_category"] == {"veo": 1, "render": 1}
        # Los errores recientes traen su categoría
        cats = {e["category"] for e in row["errors"]["recent"]}
        assert cats == {"veo", "render"}
    finally:
        from database import Job as _J
        db.query(_J).filter(_J.job_id.like("telcat%")).delete(synchronize_session=False)
        db.commit()
